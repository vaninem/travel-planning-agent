"""RAG 到已有增量向量索引的适配器。"""

import hashlib
from copy import deepcopy
from urllib.parse import quote

from ..embeddings import EmbeddingChunk, IncrementalEmbeddingIndex, SyncReport
from ..embeddings.errors import EmbeddingRequestError, IndexNotReady
from ..types import DocumentChunk, SearchHit
from .recall import index_text


class RAGIndexMismatch(RuntimeError):
    """磁盘、词法候选或向量索引的来源版本不一致。"""


class RAGEmbeddingBridge:
    def __init__(self, index: IncrementalEmbeddingIndex, repo_id: str, revision: str):
        self.index = index
        self.namespace = f"rag:{quote(repo_id, safe='')}:{quote(revision, safe='')}"

    def sync_source(self, source: str, chunks: list[DocumentChunk]) -> SyncReport:
        """同步单文件的完整快照，空列表移除该文件的索引成员。"""
        if any(chunk.source != source for chunk in chunks):
            raise ValueError("索引块必须来自同一文件")
        indexed = [
            EmbeddingChunk(chunk.id, index_text(chunk), {
                **chunk.metadata, "heading_path": chunk.heading_path, "start": chunk.start, "end": chunk.end,
            })
            for chunk in chunks
        ]
        return self.index.sync_source(self.namespace, source, indexed)

    def restore_source(self, source: str, original: str) -> list[DocumentChunk]:
        """从现有索引元数据和同一版本的原文恢复本地块。"""
        stored = self.index.store.list_source(self.index.profile_id, self.namespace, source)
        source_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        chunks = []
        for item in stored:
            metadata = deepcopy(item.metadata)
            start, end = metadata.pop("start", None), metadata.pop("end", None)
            heading = metadata.pop("heading_path", None)
            if (item.source_id != source or metadata.get("source_hash") != source_hash
                    or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(original)
                    or heading is not None and not isinstance(heading, str)):
                raise RAGIndexMismatch(f"原文或索引位置已变化，请重新同步 {source}")
            chunk = DocumentChunk(item.chunk_id, source, original[start:end], heading, start, end, metadata)
            search_text = index_text(chunk)
            if item.text != search_text or item.content_hash != hashlib.sha256(search_text.encode("utf-8")).hexdigest():
                raise RAGIndexMismatch(f"索引文本与原文不匹配，请重新同步 {source}")
            chunks.append(chunk)
        return sorted(chunks, key=lambda chunk: (chunk.start, chunk.end, chunk.id))

    def search(self, query: str, candidates: list[DocumentChunk], limit: int) -> list[SearchHit]:
        """只在允许的文件块中返回向量 Top-K，查询编码一次。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("向量检索 query 必须为非空字符串")
        if type(limit) is not int or limit <= 0:
            raise ValueError("向量检索 limit 必须为正整数")
        if not candidates:
            return []
        by_id = {(chunk.source, chunk.id): chunk for chunk in candidates}
        target = min(limit, len(by_id))
        status = self.index.status(self.namespace)
        if status.pending or status.running or status.failed:
            raise IndexNotReady(
                f"RAG 索引尚未完整就绪（pending={status.pending}, running={status.running}, failed={status.failed}）"
            )
        if status.ready < target:
            raise RAGIndexMismatch("就绪索引缺少候选块，请同步文件快照")
        vectors = self.index.client.embed([query])
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise EmbeddingRequestError("RAG 查询编码必须返回一个向量")
        query_vector = vectors[0]
        window = min(status.ready, target * 4)
        source = candidates[0].source
        source_filter = source if all(chunk.source == source for chunk in candidates) else None
        while True:
            results = []
            indexed_hits = self.index.store.search_ready(
                self.index.profile_id, self.namespace, query_vector, limit=window, source_id=source_filter,
            )
            for hit in indexed_hits:
                indexed = hit.chunk
                chunk = by_id.get((indexed.source_id, indexed.chunk_id))
                if chunk is None:
                    continue
                expected_hash = hashlib.sha256(index_text(chunk).encode("utf-8")).hexdigest()
                if (indexed.content_hash != expected_hash
                        or indexed.metadata.get("source_hash") != chunk.metadata.get("source_hash")
                        or indexed.metadata.get("start") != chunk.start or indexed.metadata.get("end") != chunk.end):
                    raise RAGIndexMismatch(f"命中索引与当前文件块不一致，请重新同步 {chunk.source}")
                results.append(SearchHit(chunk, hit.score, vector_score=hit.score))
                if len(results) == target:
                    return results
            if window == status.ready or len(indexed_hits) < window:
                raise RAGIndexMismatch("已扫描就绪范围，仍缺少候选文件块")
            window = min(status.ready, window * 2)
