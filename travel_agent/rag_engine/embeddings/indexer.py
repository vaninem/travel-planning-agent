"""E12–E15：Memory/RAG 共用索引服务；内容同步、索引执行和搜索相互分离。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from .client import EmbeddingClient, HTTPEmbeddingClient
from .config import EmbeddingConfig
from .errors import EmbeddingNotConfigured, EmbeddingRequestError, IndexNotReady
from .store import PostgresEmbeddingStore
from .types import EmbeddingChunk, EmbeddingHit, IndexStatus, ProcessReport, SyncReport, IndexedChunk, EmbeddingJob


class IncrementalEmbeddingIndex:
    def __init__(
        self, config: EmbeddingConfig,
        client: EmbeddingClient | None = None,
        store: PostgresEmbeddingStore | None = None,
    ):
        """注入模型客户端及存储；不请求接口、不读取全部来源。"""
        self.config = config
        self.client = client if client is not None else HTTPEmbeddingClient(config)
        self.store = store if store is not None else PostgresEmbeddingStore(
            config.database_url, config.database_schema, config.database_connect_timeout,
        )
        self.profile_id = config.profile_id
        self.last_process_report = ProcessReport()

    def initialize(self) -> None:
        """显式初始化存储。"""
        self.store.initialize()

    def _content_hash(self, text: str) -> str:
        """对实际编码文本做稳定 SHA-256 哈希，保留空白和大小写。"""
        byte = text.encode("utf-8")
        index = hashlib.sha256(byte)
        result = index.hexdigest()
        return result

    def sync_source(
        self, namespace: str, source_id: str, chunks: list[EmbeddingChunk],
    ) -> SyncReport:
        """对比旧快照，只为缺少缓存的新内容创建任务；本方法不请求模型。

        1. namespace 如 memory:alice、code:repo-id；source_id 为记忆 ID/相对文件名。
        2. 识别新增、修改、未变、删除；元数据/位置变更但文本不变时复用向量。
        3. 按 profile_id+content_hash 查缓存，块 ID 变化也能复用同内容向量。
        4. 用 store.commit_source 原子提交块和任务；返回真实计数。
        5. [] 是删除该来源所有块，不删除其他来源；不把全文粗略拼成一个超长输入。
        """
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace 必须是非空字符串")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id 必须是非空字符串")
        if not isinstance(chunks, list):
            raise ValueError("chunks 必须是 EmbeddingChunk 列表")
        # 写入前检查完整快照，并复制元数据，避免调用方后续修改影响待提交对象。
        prepared = []
        new_ids = set()
        for chunk in chunks:
            if not isinstance(chunk, EmbeddingChunk):
                raise ValueError("chunks 的每一项必须是 EmbeddingChunk")
            if not isinstance(chunk.id, str) or not chunk.id.strip():
                raise ValueError("块 ID 必须是非空字符串")
            if chunk.id in new_ids:
                raise ValueError("同一来源的块 ID 不能重复")
            if not isinstance(chunk.text, str) or not chunk.text.strip():
                raise ValueError("块正文必须是非空字符串")
            if not isinstance(chunk.metadata, dict) or any(not isinstance(key, str) for key in chunk.metadata):
                raise ValueError("块 metadata 必须是字符串键字典")
            try:
                json.dumps(chunk.metadata, allow_nan=False)
                metadata = deepcopy(chunk.metadata)
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise ValueError("块 metadata 必须可保存为有限数值的 JSON 数据") from None
            prepared.append(replace(chunk, metadata=metadata))
            new_ids.add(chunk.id)

        old_chunks = self.store.list_source(self.profile_id, namespace, source_id)
        old_by_id = {chunk.chunk_id: chunk for chunk in old_chunks}
        new_chunks = []
        new_jobs = []
        report = SyncReport()
        for chunk in prepared:
            old = old_by_id.get(chunk.id)
            content_hash = self._content_hash(chunk.text)
            if old is not None and old.content_hash == content_hash:
                # 内容不变：保留状态；对应的有效旧任务由存储层保留。
                unchanged_chunk = replace(old, metadata=dict(chunk.metadata))
                new_chunks.append(unchanged_chunk)
                report.unchanged += 1
            else:
                if old is None:
                    report.created += 1
                else:
                    report.updated += 1
                try_cache = self.store.get_cached(self.profile_id, content_hash)
                if try_cache is None:
                    new_chunk = IndexedChunk(
                        profile_id = self.profile_id,
                        namespace = namespace,
                        source_id = source_id,
                        chunk_id = chunk.id,
                        text = chunk.text,
                        content_hash = content_hash,
                        metadata =dict(chunk.metadata),
                        state = "pending"
                    )
                    new_job = EmbeddingJob(
                        id = f"job_{uuid4().hex}",
                        chunk= new_chunk
                    ) 
                    new_jobs.append(new_job)
                    report.queued += 1
                else:
                    report.cache_hits += 1
                    new_chunk = IndexedChunk(
                        profile_id = self.profile_id,
                        namespace = namespace,
                        source_id = source_id,
                        chunk_id = chunk.id,
                        text = chunk.text,
                        content_hash = content_hash,
                        metadata = dict(chunk.metadata),
                        state = "ready"
                    )
                new_chunks.append(new_chunk)

        report.deleted = len(set(old_by_id) - new_ids)
        self.store.commit_source(
            self.profile_id,
            namespace,
            source_id,
            new_chunks,
            new_jobs,
        )
        return report

    def delete_source(self, namespace: str, source_id: str) -> SyncReport:
        """通过空快照删除成员和任务；共享内容缓存可保留。"""
        return self.sync_source(namespace, source_id, [])

    def process_pending(self, limit: int | None = None, *, namespace: str | None = None) -> int:
        """独立索引执行入口；可由显式命令或后台 worker 调用。

        认领任务→再次查缓存→批量生成缺失向量→逐条提交；同批相同 hash 去重。
        网络放在事务外；过期任务不能覆盖新版本；重启后继续持久任务。
        HTTP 客户端未配置时在领取前明确失败；不把任务标 ready。
        返回成功提交的块数量；详细计数保存在 last_process_report。
        存储异常直接上抛，不当成模型请求失败；中断后的租约由队列机制处理。
        """
        limit = self.config.batch_size if limit is None else limit
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        self.last_process_report = report = ProcessReport()
        if isinstance(self.client, HTTPEmbeddingClient) and not self.client.config.enabled:
            raise EmbeddingNotConfigured("请先配置嵌入服务，再领取索引任务")
        scope = {} if namespace is None else {"namespace": namespace}
        jobs = self.store.claim_jobs(
            self.profile_id, limit, self.config.lease_seconds, self.config.max_attempts, **scope,
        )
        report.claimed = len(jobs)
        jobs_by_hash = {}
        for job in jobs:
            content_hash = job.chunk.content_hash
            if content_hash not in jobs_by_hash:
                jobs_by_hash[content_hash] = []
            jobs_by_hash[content_hash].append(job)
        missing_hashes = []
        vectors_by_hash = {}
        for content_hash, jobs_list in jobs_by_hash.items():
            try_cache = self.store.get_cached(self.profile_id, content_hash)
            if try_cache is None:
                missing_hashes.append(content_hash)
            else:
                vectors_by_hash[content_hash] = try_cache
                report.cache_hits += len(jobs_list)

        # 缓存命中先提交，不等待网络；同哈希的每条任务仍分别检查租约。
        for content_hash, vector in vectors_by_hash.items():
            for job in jobs_by_hash[content_hash]:
                if self.store.complete_job(job, vector):
                    report.completed += 1
                else:
                    report.stale += 1

        # 执行器按批调用并立即提交；后一批失败不会丢弃前一批已完成结果。
        for start in range(0, len(missing_hashes), self.config.batch_size):
            batch_hashes = missing_hashes[start:start + self.config.batch_size]
            texts = [jobs_by_hash[digest][0].chunk.text for digest in batch_hashes]
            try:
                vectors = self.client.embed(texts)
                if not isinstance(vectors, list) or len(vectors) != len(batch_hashes):
                    raise EmbeddingRequestError("返回向量数量与本批正文数量不一致")
            except EmbeddingRequestError as exc:
                report.request_errors += 1
                report.generation_errors += sum(len(jobs_by_hash[digest]) for digest in batch_hashes)
                for content_hash in batch_hashes:
                    for job in jobs_by_hash[content_hash]:
                        self.store.fail_job(
                            job, error_code=exc.error_code,
                            retryable=exc.retryable and job.attempts < self.config.max_attempts,
                        )
                continue

            report.generated += len(vectors)
            vectors_by_hash.update(zip(batch_hashes, vectors))
            for content_hash in batch_hashes:
                vector = vectors_by_hash[content_hash]
                for job in jobs_by_hash[content_hash]:
                    if self.store.complete_job(job, vector):
                        report.completed += 1
                    else:
                        report.stale += 1
        return report.completed

    def prepare_namespace(self, namespace: str) -> IndexStatus:
        """处理该命名空间当前可领取的任务，遇到接口错误时返回状态。"""
        while True:
            self.process_pending(namespace=namespace)
            report = self.last_process_report
            if report.request_errors or report.claimed < self.config.batch_size:
                return self.status(namespace)

    def status(self, namespace: str) -> IndexStatus:
        """显式查看当前块的索引进度。"""
        return self.store.status(self.profile_id, namespace)

    def search(
        self, query: str, namespace: str, limit: int = 5, source_id: str | None = None,
    ) -> list[EmbeddingHit]:
        """仅编码 query，再调用 store.search_ready 在 pgvector 中查询。

        绝不调用 sync_source/process_pending，也不在首次查询时生成全部文档向量。
        在数据库中筛选、余弦排序及 LIMIT，不在 Python 中加载全部文档向量。
        list_ready 只用于调试/验收，正式检索路径使用 search_ready。
        维度/范数检查、稳定排序和 Top-k；索引不完整时明确提示，不能伪报全库检索。
        本服务只做稠密检索；TF-IDF/BM25 与 RRF 留给之后的 Memory/RAG 作业。
        """
        for name, value in (("query", query), ("namespace", namespace)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        if source_id is not None and (not isinstance(source_id, str) or not source_id.strip()):
            raise ValueError("source_id 必须是非空字符串或 None")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        # 首版按整个 namespace 判断就绪；source_id 只进一步筛选候选块。
        status = self.status(namespace)
        if status.pending or status.running or status.failed:
            raise IndexNotReady(
                f"索引尚未完整就绪（pending={status.pending}, running={status.running}, failed={status.failed}）"
            )
        if status.ready == 0:
            return []
        vectors = self.client.embed([query])
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise EmbeddingRequestError("查询编码必须返回一个向量")
        query_vector = vectors[0]
        hits = self.store.search_ready(self.profile_id, namespace, query_vector, limit, source_id)
        return hits
