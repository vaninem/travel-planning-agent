"""E13–E15：PostgreSQL＋pgvector 的持久缓存、索引任务和相似度查询骨架。"""

import math
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from numbers import Real
import re
from typing import Any, Callable
from uuid import uuid4

import psycopg
from psycopg import sql
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from pgvector import Vector

from ..errors import ExercisePending
from .errors import EmbeddingStorageError
from .types import EmbeddingHit, EmbeddingJob, IndexedChunk, IndexStatus


class PostgresEmbeddingStore:
    def __init__(
        self, database_url: str, schema: str = "agent_embeddings", connect_timeout: int = 10,
        connection_factory: Callable[..., Any] | None = None,
    ):
        """只保存配置，不连接数据库；可注入假连接用于单元测试。

        database_url 含口令，日志/异常不直接输出它。schema 需要使用
        psycopg.sql.Identifier 组合，SQL 值一律参数化，不拼接原始输入。
        """
        self.database_url = database_url
        self.schema = schema
        self.connect_timeout = connect_timeout
        self.connection_factory = connection_factory

    def connect(self):
        """按需建立 psycopg 3 连接，使用方通过 with 关闭及提交/回滚。

        支持 connection_factory；默认使用 psycopg.connect 创建连接。
        用 pgvector.psycopg.register_vector 注册已启用扩展的连接；向量值可
        通过 pgvector.Vector 适配，不能把 list 的数组适配误当 vector 类型。
        首次创建扩展的连接需先完成 CREATE EXTENSION 再注册。
        """
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise ValueError("请配置 DATABASE_URL")
        if type(self.connect_timeout) is not int or self.connect_timeout <= 0:
            raise ValueError("数据库连接超时必须是正整数")
        factory = self.connection_factory if self.connection_factory is not None else psycopg.connect
        conn = None
        try:
            conn = factory(self.database_url, connect_timeout=self.connect_timeout)
            register_vector(conn)
            return conn
        except Exception as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass  # 清理错误不能覆盖最初的连接或注册错误。
            raise EmbeddingStorageError(
                f"数据库连接或向量类型注册失败（{type(exc).__name__}）；请检查服务、配置和 vector 扩展"
            ) from None

    def initialize(self) -> None:
        """启用 vector 扩展，创建专属 schema、四张表和普通索引，可重复执行。

        四表：profiles(模型标识/维度)、vectors(profile_id/content_hash/embedding)、
        chunks(profile_id/namespace/source_id/chunk_id/text/hash/metadata/state)、
        jobs(任务/版本/租约/尝试次数/错误)。唯一键和索引见学习文档。
        向量列使用 pgvector 的 vector 类型，元数据使用 JSONB，时间用 TIMESTAMPTZ。
        先实现数据库内精确余弦排序；后续按模型/维度建立部分 HNSW 索引。
        不把 API Key 存入数据库；不要删除整个 public schema 或其他项目的数据。
        """
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise ValueError("请配置 DATABASE_URL")
        if type(self.connect_timeout) is not int or self.connect_timeout <= 0:
            raise ValueError("数据库连接超时必须是正整数")
        if not isinstance(self.schema, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.schema):
            raise ValueError("数据库 schema 必须是最多 63 个字符的小写字母、数字、下划线标识符")
        factory = self.connection_factory if self.connection_factory is not None else psycopg.connect
        try:
            # 不调用 connect()：首次初始化时扩展可能还不存在。
            with factory(self.database_url, connect_timeout=self.connect_timeout) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                register_vector(conn)
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.schema),
                ))

                profiles_query = sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {} (
                        profile_id TEXT PRIMARY KEY,
                        dimensions INTEGER CHECK (dimensions > 0)
                    )
                """).format(sql.Identifier(self.schema, "profiles"))
                conn.execute(profiles_query)

                vectors_query = sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {} (
                        profile_id TEXT NOT NULL REFERENCES {} (profile_id),
                        content_hash TEXT NOT NULL,
                        embedding VECTOR NOT NULL,
                        PRIMARY KEY (profile_id, content_hash)
                    )
                """).format(
                    sql.Identifier(self.schema, "vectors"),
                    sql.Identifier(self.schema, "profiles"),
                )
                conn.execute(vectors_query)

                chunks_query = sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {} (
                        profile_id TEXT NOT NULL REFERENCES {} (profile_id),
                        namespace TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        state TEXT NOT NULL DEFAULT 'pending',
                        CHECK (jsonb_typeof(metadata) = 'object'),
                        CHECK (state IN ('pending', 'running', 'ready', 'failed')),
                        PRIMARY KEY (profile_id, namespace, source_id, chunk_id)
                    )
                """).format(
                    sql.Identifier(self.schema, "chunks"),
                    sql.Identifier(self.schema, "profiles"),
                )
                conn.execute(chunks_query)

                jobs_query = sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {} (
                        job_id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        lease_token TEXT,
                        lease_expires_at TIMESTAMPTZ,
                        retry_after TIMESTAMPTZ,
                        error_code TEXT,
                        CHECK (state IN ('pending', 'running', 'ready', 'failed')),
                        CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
                        CHECK (state <> 'running' OR lease_token IS NOT NULL),
                        UNIQUE (profile_id, namespace, source_id, chunk_id, content_hash),
                        FOREIGN KEY (profile_id, namespace, source_id, chunk_id)
                            REFERENCES {} (profile_id, namespace, source_id, chunk_id)
                            ON DELETE CASCADE
                    )
                """).format(
                    sql.Identifier(self.schema, "jobs"),
                    sql.Identifier(self.schema, "chunks"),
                )
                conn.execute(jobs_query)

                # 精确向量搜索基线；这里只建立筛选及任务认领所需的 B-tree 索引。
                conn.execute(sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {} ON {} (profile_id, namespace, state)
                """).format(
                    sql.Identifier("chunks_namespace_state_idx"), sql.Identifier(self.schema, "chunks"),
                ))
                conn.execute(sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {} ON {} (profile_id, content_hash)
                """).format(
                    sql.Identifier("chunks_content_hash_idx"), sql.Identifier(self.schema, "chunks"),
                ))
                conn.execute(sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {} ON {} (profile_id, state, retry_after)
                """).format(
                    sql.Identifier("jobs_pending_idx"), sql.Identifier(self.schema, "jobs"),
                ))
                conn.execute(sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {} ON {} (profile_id, lease_expires_at)
                    WHERE state = 'running'
                """).format(
                    sql.Identifier("jobs_expired_lease_idx"), sql.Identifier(self.schema, "jobs"),
                ))
        except Exception as exc:
            # psycopg 的 with 在异常时回滚并关闭连接；不输出原始连接/SQL 数据。
            raise EmbeddingStorageError(
                f"嵌入数据库初始化失败（{type(exc).__name__}）；请检查服务、权限、扩展及表结构"
            ) from None

    def get_cached(self, profile_id: str, content_hash: str) -> list[float] | None:
        """查询同模型同内容的向量；只在确实无记录时返回 None。"""
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("profile_id 必须是非空字符串")
        if not isinstance(content_hash, str) or not content_hash.strip():
            raise ValueError("content_hash 必须是非空字符串")
        try:
            with self.connect() as conn:
                query = sql.SQL("""
                    SELECT embedding FROM {}
                    WHERE profile_id = %s AND content_hash = %s
                """).format(sql.Identifier(self.schema, "vectors"))
                cursor = conn.execute(query, (profile_id, content_hash))
                row = cursor.fetchone()
            if row is None:
                return None

            # 驱动版本可能返回 Vector 或 NumPy 数组；统一为普通数值列表。
            try:
                if len(row) != 1 or isinstance(row[0], (str, bytes, dict)):
                    raise ValueError
                values = row[0].to_list() if isinstance(row[0], Vector) else list(row[0])
                if not values or any(isinstance(value, bool) or not isinstance(value, Real) for value in values):
                    raise ValueError
                vector = [float(value) for value in values]
                if not all(math.isfinite(value) for value in vector) or not any(vector):
                    raise ValueError
            except (TypeError, ValueError, OverflowError, IndexError, KeyError):
                raise EmbeddingStorageError("缓存向量损坏：必须是非空、有限、非零的一维数值向量") from None
            return vector
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"读取向量缓存失败（{type(exc).__name__}）") from None


    def list_source(self, profile_id: str, namespace: str, source_id: str) -> list[IndexedChunk]:
        """获取指定来源的当前块，按 chunk_id 排序；仅空来源返回空列表。"""
        for name, value in (("profile_id", profile_id), ("namespace", namespace), ("source_id", source_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        try:
            with self.connect() as conn:
                results = []
                query = sql.SQL("""
                    SELECT profile_id, namespace, source_id, chunk_id,
                           text, content_hash, metadata, state
                    FROM {}
                    WHERE profile_id = %s AND namespace = %s AND source_id = %s
                    ORDER BY chunk_id
                """).format(sql.Identifier(self.schema, "chunks"))
                cursor = conn.execute(query, (profile_id, namespace, source_id))
                columns = [column.name for column in cursor.description or ()]
                expected = {"profile_id", "namespace", "source_id", "chunk_id", "text", "content_hash", "metadata", "state"}
                if len(columns) != len(expected) or set(columns) != expected:
                    raise EmbeddingStorageError("来源块查询的返回列不完整或重复")
                chunks = cursor.fetchall()
                for chunk in chunks:
                    if len(chunk) != len(columns):
                        raise EmbeddingStorageError("来源块记录的字段数量与查询列不一致")
                    chunk_dict = dict(zip(columns, chunk))
                    item = IndexedChunk(**chunk_dict)
                    if (item.profile_id, item.namespace, item.source_id) != (profile_id, namespace, source_id):
                        raise EmbeddingStorageError("来源块记录超出本次查询范围")
                    if any(not isinstance(value, str) or not value.strip() for value in (item.chunk_id, item.text, item.content_hash)):
                        raise EmbeddingStorageError("来源块记录的 ID、正文或内容哈希损坏")
                    if not isinstance(item.metadata, dict) or item.state not in ("pending", "running", "ready", "failed"):
                        raise EmbeddingStorageError("来源块记录的元数据或状态损坏")
                    item.metadata = deepcopy(item.metadata)
                    results.append(item)
            return results
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"读取来源块失败（{type(exc).__name__}）") from None
    

    def commit_source(
        self, profile_id: str, namespace: str, source_id: str,
        chunks: list[IndexedChunk], jobs: list[EmbeddingJob],
    ) -> None:
        """一个事务提交该来源的完整新快照及增量任务。

        仅对该来源差异更新：UPSERT 当前块，删除不在新快照的块和旧版本任务。
        未变块的有效旧任务与租约必须保留，不能先 DELETE 整个来源再插回，
        否则 jobs 的 ON DELETE CASCADE 会误删未变块的任务。保留其他来源/模型。
        不能先更新块再漏写任务。删除成员不必删除可被其他块复用的内容缓存。
        使用参数化 SQL 与 ON CONFLICT；事务失败回滚，不提交半个来源。
        """
        scope = (profile_id, namespace, source_id)
        for name, value in zip(("profile_id", "namespace", "source_id"), scope):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        if not isinstance(chunks, list) or not isinstance(jobs, list):
            raise ValueError("chunks 和 jobs 必须是列表")

        # 完整检查并复制后才开始写数据库，归属不一致时拒绝而不是覆盖字段。
        prepared = {}
        for chunk in chunks:
            if not isinstance(chunk, IndexedChunk):
                raise ValueError("chunks 中的每一项必须是 IndexedChunk")
            if (chunk.profile_id, chunk.namespace, chunk.source_id) != scope:
                raise ValueError("块不属于本次提交的来源")
            if any(not isinstance(value, str) or not value.strip() for value in (chunk.chunk_id, chunk.text, chunk.content_hash)):
                raise ValueError("块 ID、正文和内容哈希必须是非空字符串")
            if chunk.chunk_id in prepared:
                raise ValueError("新快照中的块 ID 不能重复")
            if chunk.content_hash != hashlib.sha256(chunk.text.encode("utf-8")).hexdigest():
                raise ValueError("块的内容哈希与正文不一致")
            if chunk.state not in ("pending", "running", "ready", "failed"):
                raise ValueError("块状态不受支持")
            if not isinstance(chunk.metadata, dict) or any(not isinstance(key, str) for key in chunk.metadata):
                raise ValueError("块 metadata 必须是字符串键字典")
            try:
                json.dumps(chunk.metadata, allow_nan=False)
                metadata = deepcopy(chunk.metadata)
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise ValueError("块 metadata 必须可保存为有限数值的 JSON 数据") from None
            prepared[chunk.chunk_id] = replace(chunk, metadata=metadata)

        new_jobs = []
        job_ids, job_versions = set(), set()
        for job in jobs:
            if not isinstance(job, EmbeddingJob) or not isinstance(job.chunk, IndexedChunk):
                raise ValueError("jobs 中的每一项必须是包含 IndexedChunk 的 EmbeddingJob")
            if not isinstance(job.id, str) or not job.id.strip() or job.id in job_ids:
                raise ValueError("新任务 ID 必须是非空且不重复的字符串")
            if (job.chunk.profile_id, job.chunk.namespace, job.chunk.source_id) != scope:
                raise ValueError("任务不属于本次提交的来源")
            current = prepared.get(job.chunk.chunk_id)
            if current is None or current.content_hash != job.chunk.content_hash or current.text != job.chunk.text:
                raise ValueError("任务必须对应新快照中的同一块、同一版正文")
            if current.state != "pending":
                raise ValueError("新增任务只能对应 pending 块")
            if type(job.attempts) is not int or job.attempts != 0 or job.lease_token is not None or job.lease_expires_at is not None:
                raise ValueError("新增任务必须尚未尝试和领取")
            version = (current.chunk_id, current.content_hash)
            if version in job_versions:
                raise ValueError("同块同版本不能重复提交新任务")
            job_ids.add(job.id)
            job_versions.add(version)
            new_jobs.append(replace(job, chunk=current))

        try:
            with self.connect() as conn:
                with conn.transaction():
                    # 按 schema/模型/来源加事务锁；即使空来源也能串行提交。
                    lock_identity = json.dumps([self.schema, *scope], ensure_ascii=False).encode("utf-8")
                    lock_key = int.from_bytes(hashlib.sha256(lock_identity).digest()[:8], "big", signed=True)
                    conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (lock_key,))
                    query = sql.SQL("""
                        INSERT INTO {} (profile_id) VALUES (%s)
                        ON CONFLICT (profile_id) DO NOTHING
                    """).format(sql.Identifier(self.schema, "profiles"))
                    conn.execute(query, (profile_id,))

                    upsert_chunk = sql.SQL("""
                        INSERT INTO {} AS existing (
                            profile_id, namespace, source_id, chunk_id,
                            text, content_hash, metadata, state
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (profile_id, namespace, source_id, chunk_id)
                        DO UPDATE SET
                            text = EXCLUDED.text,
                            content_hash = EXCLUDED.content_hash,
                            metadata = EXCLUDED.metadata,
                            state = CASE
                                WHEN existing.content_hash = EXCLUDED.content_hash THEN existing.state
                                ELSE EXCLUDED.state
                            END
                        RETURNING state
                    """).format(sql.Identifier(self.schema, "chunks"))
                    delete_old_jobs = sql.SQL("""
                        DELETE FROM {}
                        WHERE profile_id = %s AND namespace = %s AND source_id = %s
                          AND chunk_id = %s AND content_hash <> %s
                    """).format(sql.Identifier(self.schema, "jobs"))
                    saved_states = {}
                    for chunk in prepared.values():
                        row = conn.execute(upsert_chunk, (
                            *scope, chunk.chunk_id, chunk.text, chunk.content_hash,
                            Jsonb(chunk.metadata), chunk.state,
                        )).fetchone()
                        if row is None or row[0] not in ("pending", "running", "ready", "failed"):
                            raise EmbeddingStorageError("保存块后未返回有效状态")
                        saved_states[chunk.chunk_id] = row[0]
                        conn.execute(delete_old_jobs, (*scope, chunk.chunk_id, chunk.content_hash))

                    delete_missing = sql.SQL("""
                        DELETE FROM {}
                        WHERE profile_id = %s AND namespace = %s AND source_id = %s
                          AND NOT (chunk_id = ANY(%s::text[]))
                    """).format(sql.Identifier(self.schema, "chunks"))
                    conn.execute(delete_missing, (*scope, list(prepared)))

                    insert_job = sql.SQL("""
                        INSERT INTO {} (
                            job_id, profile_id, namespace, source_id, chunk_id, content_hash
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (profile_id, namespace, source_id, chunk_id, content_hash)
                        DO NOTHING
                    """).format(sql.Identifier(self.schema, "jobs"))
                    for job in new_jobs:
                        # 计划产生后，worker 可能已推进了状态；不要再为它新建任务。
                        if saved_states[job.chunk.chunk_id] == "pending":
                            conn.execute(insert_job, (job.id, *scope, job.chunk.chunk_id, job.chunk.content_hash))

                    # 未变块可以不带新任务，但数据库里必须仍有同版本有效任务。
                    orphan_query = sql.SQL("""
                        SELECT c.chunk_id FROM {} AS c
                        WHERE c.profile_id = %s AND c.namespace = %s AND c.source_id = %s
                          AND c.state IN ('pending', 'running')
                          AND NOT EXISTS (
                              SELECT 1 FROM {} AS j
                              WHERE j.profile_id = c.profile_id AND j.namespace = c.namespace
                                AND j.source_id = c.source_id AND j.chunk_id = c.chunk_id
                                AND j.content_hash = c.content_hash AND j.state IN ('pending', 'running')
                          )
                        LIMIT 1
                    """).format(sql.Identifier(self.schema, "chunks"), sql.Identifier(self.schema, "jobs"))
                    if conn.execute(orphan_query, scope).fetchone() is not None:
                        raise EmbeddingStorageError("待处理块缺少同版本有效任务，来源快照未提交")
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"提交来源快照失败（{type(exc).__name__}）") from None

    def claim_jobs(
        self, profile_id: str, limit: int, lease_seconds: int, max_attempts: int,
        *, namespace: str | None = None,
    ) -> list[EmbeddingJob]:
        """事务认领 pending 或租约过期任务，增加尝试次数与租约。

        防止两个 worker 同时认领；超过 max_attempts 的任务不再自动重试。
        可使用 FOR UPDATE SKIP LOCKED，租约时间采用数据库时间。
        网络请求必须在事务外执行，不能持有数据库写锁等待模型。
        """
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("profile_id 必须是非空字符串")
        if namespace is not None and (not isinstance(namespace, str) or not namespace.strip()):
            raise ValueError("namespace 必须是非空字符串")
        for name, value in (("limit", limit), ("lease_seconds", lease_seconds), ("max_attempts", max_attempts)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        try:
            with self.connect() as conn:
                with conn.transaction():
                    # 一起锁住当前版本的任务和块，跳过正在被其他事务修改的记录。
                    search_jobs = sql.SQL('''
                        SELECT j.job_id, j.profile_id, j.namespace, j.source_id,
                               j.chunk_id, j.content_hash
                        FROM {} AS j
                        JOIN {} AS c
                          ON c.profile_id = j.profile_id AND c.namespace = j.namespace
                         AND c.source_id = j.source_id AND c.chunk_id = j.chunk_id
                         AND c.content_hash = j.content_hash
                        WHERE j.profile_id = %s AND j.attempts < %s
                          {}
                          AND c.state IN ('pending', 'running')
                          AND (
                              (j.state = 'pending' AND (j.retry_after IS NULL OR j.retry_after <= NOW()))
                              OR (j.state = 'running' AND j.lease_expires_at <= NOW())
                          )
                        ORDER BY j.job_id
                        LIMIT %s
                        FOR UPDATE OF j, c SKIP LOCKED
                    ''').format(
                        sql.Identifier(self.schema, "jobs"), sql.Identifier(self.schema, "chunks"),
                        sql.SQL("AND j.namespace = %s") if namespace is not None else sql.SQL(""),
                    )
                    scope = () if namespace is None else (namespace,)
                    cursor = conn.execute(search_jobs, (profile_id, max_attempts, *scope, limit))
                    columns = [column.name for column in cursor.description]
                    jobs_available = [dict(zip(columns, row)) for row in cursor.fetchall()]
                    update_job = sql.SQL("""
                        UPDATE {}
                        SET state = 'running', attempts = attempts + 1,
                            lease_token = %s,
                            lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                            retry_after = NULL, error_code = NULL
                        WHERE job_id = %s
                        RETURNING attempts, lease_token, lease_expires_at
                    """).format(sql.Identifier(self.schema, "jobs"))
                    update_chunk = sql.SQL("""
                        UPDATE {} SET state = 'running'
                        WHERE profile_id = %s AND namespace = %s AND source_id = %s
                          AND chunk_id = %s AND content_hash = %s
                        RETURNING profile_id, namespace, source_id, chunk_id,
                                  text, content_hash, metadata, state
                    """).format(sql.Identifier(self.schema, "chunks"))
                    new_jobs = []
                    for job in jobs_available:
                        lease_token = uuid4().hex
                        updated = conn.execute(update_job, (lease_token, lease_seconds, job["job_id"])).fetchone()
                        if updated is None:
                            raise EmbeddingStorageError("领取任务时记录已不存在")
                        attempts, saved_token, lease_expires_at = updated
                        if (type(attempts) is not int or not 1 <= attempts <= max_attempts
                                or saved_token != lease_token or not isinstance(lease_expires_at, datetime)
                                or lease_expires_at.utcoffset() is None):
                            raise EmbeddingStorageError("任务领取结果或租约格式损坏")
                        scope = tuple(job[key] for key in ("profile_id", "namespace", "source_id", "chunk_id", "content_hash"))
                        chunk_row = conn.execute(update_chunk, scope).fetchone()
                        if chunk_row is None:
                            raise EmbeddingStorageError("领取任务时找不到同版本块，整批领取回滚")
                        chunk = IndexedChunk(*chunk_row)
                        if ((chunk.profile_id, chunk.namespace, chunk.source_id, chunk.chunk_id, chunk.content_hash) != scope
                                or chunk.profile_id != profile_id or chunk.state != "running"
                                or not isinstance(chunk.metadata, dict)):
                            raise EmbeddingStorageError("领取的块与任务不一致")
                        chunk.metadata = deepcopy(chunk.metadata)
                        new_jobs.append(EmbeddingJob(
                            job["job_id"], chunk, attempts, saved_token, lease_expires_at.timestamp(),
                        ))
            return new_jobs
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"领取嵌入任务失败（{type(exc).__name__}）") from None

    def complete_job(self, job: EmbeddingJob, vector: list[float]) -> bool:
        """原子保存向量并完成任务；已失效任务返回 False，存储失败抛异常。

        对来源加与 commit_source 相同的事务锁，然后按块、任务顺序锁行。
        同模型维度通过 profiles 行锁串行检查；等待锁后使用数据库实时时钟
        复核租约，不能用事务开始时固定的 NOW() 让过期结果通过。
        """
        if not isinstance(job, EmbeddingJob) or not isinstance(job.chunk, IndexedChunk):
            raise ValueError("job 必须是包含 IndexedChunk 的 EmbeddingJob")
        chunk = job.chunk
        scope = (chunk.profile_id, chunk.namespace, chunk.source_id, chunk.chunk_id, chunk.content_hash)
        if any(not isinstance(value, str) or not value.strip() for value in (job.id, job.lease_token, *scope)):
            raise ValueError("任务 ID、租约令牌及块身份必须是非空字符串")
        if (not isinstance(vector, list) or not vector
                or any(isinstance(value, bool) or not isinstance(value, Real) for value in vector)):
            raise ValueError("向量必须是非空的一维数值列表")
        try:
            values = [float(value) for value in vector]
            if any(not math.isfinite(value) or abs(value) > 3.4028234663852886e38 for value in values):
                raise ValueError
            embedding = Vector(values)
            if not any(embedding.to_list()):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError("向量必须能表示为有限、非零的 float32 向量") from None

        try:
            with self.connect() as conn:
                with conn.transaction():
                    lock_identity = json.dumps([self.schema, *scope[:3]], ensure_ascii=False).encode("utf-8")
                    lock_key = int.from_bytes(hashlib.sha256(lock_identity).digest()[:8], "big", signed=True)
                    conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (lock_key,))
                    lock_chunk = sql.SQL("""
                        SELECT content_hash, state FROM {}
                        WHERE profile_id = %s AND namespace = %s AND source_id = %s
                          AND chunk_id = %s
                        FOR UPDATE
                    """).format(sql.Identifier(self.schema, "chunks"))
                    current = conn.execute(lock_chunk, scope[:4]).fetchone()
                    if current is None or current != (chunk.content_hash, "running"):
                        return False

                    search_query = sql.SQL("""
                        SELECT content_hash, lease_token FROM {}
                        WHERE job_id = %s AND profile_id = %s AND namespace = %s
                          AND source_id = %s AND chunk_id = %s AND content_hash = %s
                          AND state = 'running' AND lease_expires_at > clock_timestamp()
                        FOR UPDATE
                    """).format(sql.Identifier(self.schema, "jobs"))
                    job_scope = (job.id, *scope)
                    row = conn.execute(search_query, job_scope).fetchone()
                    if row is None or row != (chunk.content_hash, job.lease_token):
                        return False

                    dimensions_query = sql.SQL("""
                        SELECT dimensions FROM {} WHERE profile_id = %s FOR UPDATE
                    """).format(sql.Identifier(self.schema, "profiles"))
                    row = conn.execute(dimensions_query, (chunk.profile_id,)).fetchone()
                    if row is None:
                        raise EmbeddingStorageError("任务对应的 profile 不存在")
                    dimensions = row[0]
                    if dimensions is not None and dimensions != len(values):
                        raise EmbeddingStorageError("向量维度与该 profile 已记录的维度不一致")

                    # 等待维度锁期间租约也可能过期；任何写入之前再次确认。
                    # 先更新任务便于在拒绝时直接返回；后续失败会回滚该更新。
                    finish_job = sql.SQL("""
                        UPDATE {} SET state = 'ready', lease_token = NULL,
                            lease_expires_at = NULL, retry_after = NULL, error_code = NULL
                        WHERE job_id = %s AND profile_id = %s AND namespace = %s
                          AND source_id = %s AND chunk_id = %s AND content_hash = %s
                          AND lease_token = %s AND state = 'running'
                          AND lease_expires_at > clock_timestamp()
                        RETURNING job_id
                    """).format(sql.Identifier(self.schema, "jobs"))
                    if conn.execute(finish_job, (*job_scope, job.lease_token)).fetchone() is None:
                        return False

                    if dimensions is None:
                        update_dimensions = sql.SQL("""
                            UPDATE {} SET dimensions = %s WHERE profile_id = %s
                        """).format(sql.Identifier(self.schema, "profiles"))
                        conn.execute(update_dimensions, (len(values), chunk.profile_id))
                    update_vector = sql.SQL("""
                        INSERT INTO {} (profile_id, content_hash, embedding)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (profile_id, content_hash) DO NOTHING
                    """).format(sql.Identifier(self.schema, "vectors"))
                    conn.execute(update_vector, (chunk.profile_id, chunk.content_hash, embedding))
                    finish_chunk = sql.SQL("""
                        UPDATE {} SET state = 'ready'
                        WHERE profile_id = %s AND namespace = %s AND source_id = %s
                          AND chunk_id = %s AND content_hash = %s AND state = 'running'
                        RETURNING chunk_id
                    """).format(sql.Identifier(self.schema, "chunks"))
                    if conn.execute(finish_chunk, scope).fetchone() is None:
                        raise EmbeddingStorageError("完成任务时当前块版本不一致，提交已回滚")
            return True
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"提交嵌入结果失败（{type(exc).__name__}）") from None

    def fail_job(self, job: EmbeddingJob, error_code: str, retryable: bool) -> None:
        """同租约、同版本失败处理；可重试任务等待 5 秒，否则置 failed。

        是否超过重试次数由调用方结合 max_attempts 决定，再传入 retryable。
        attempts 已在领取时增加，此处不再增加；过期 worker 的报告直接忽略。
        """
        if not isinstance(job, EmbeddingJob) or not isinstance(job.chunk, IndexedChunk):
            raise ValueError("job 必须是包含 IndexedChunk 的 EmbeddingJob")
        chunk = job.chunk
        scope = (chunk.profile_id, chunk.namespace, chunk.source_id, chunk.chunk_id, chunk.content_hash)
        if any(not isinstance(value, str) or not value.strip() for value in (job.id, job.lease_token, *scope)):
            raise ValueError("任务 ID、租约令牌及块身份必须是非空字符串")
        if type(retryable) is not bool:
            raise ValueError("retryable 必须是布尔值")
        if not isinstance(error_code, str) or re.fullmatch(r"[A-Za-z0-9_]{1,64}", error_code) is None:
            raise ValueError("error_code 仅支持 1 至 64 位字母、数字和下划线，不接受原始异常文本")
        new_state = "pending" if retryable else "failed"
        try:
            with self.connect() as conn:
                with conn.transaction():
                    # 与来源同步、完成任务采用相同的来源锁和块→任务的行锁顺序。
                    lock_identity = json.dumps([self.schema, *scope[:3]], ensure_ascii=False).encode("utf-8")
                    lock_key = int.from_bytes(hashlib.sha256(lock_identity).digest()[:8], "big", signed=True)
                    conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (lock_key,))
                    search_chunk = sql.SQL("""
                        SELECT state, content_hash FROM {}
                        WHERE profile_id = %s AND namespace = %s AND source_id = %s AND chunk_id = %s
                        FOR UPDATE
                    """).format(sql.Identifier(self.schema, "chunks"))
                    row = conn.execute(search_chunk, scope[:4]).fetchone()
                    if row is None or row != ("running", chunk.content_hash):
                        return
                    search_job = sql.SQL("""
                        SELECT lease_token FROM {}
                        WHERE job_id = %s AND profile_id = %s AND namespace = %s
                          AND source_id = %s AND chunk_id = %s AND content_hash = %s
                          AND state = 'running' AND lease_expires_at > clock_timestamp()
                        FOR UPDATE
                    """).format(sql.Identifier(self.schema, "jobs"))
                    job_scope = (job.id, *scope)
                    row = conn.execute(search_job, job_scope).fetchone()
                    if row is None or row[0] != job.lease_token:
                        return
                    update_job = sql.SQL("""
                        UPDATE {} SET state = %s,
                            retry_after = CASE WHEN %s THEN clock_timestamp() + INTERVAL '5 seconds' ELSE NULL END,
                            error_code = %s, lease_token = NULL, lease_expires_at = NULL
                        WHERE job_id = %s AND profile_id = %s AND namespace = %s
                          AND source_id = %s AND chunk_id = %s AND content_hash = %s
                          AND lease_token = %s AND state = 'running'
                          AND lease_expires_at > clock_timestamp()
                        RETURNING job_id
                    """).format(sql.Identifier(self.schema, "jobs"))
                    # 写入前再次判断实际有效期，拒绝在等待锁期间过期的结果。
                    updated = conn.execute(update_job, (new_state, retryable, error_code, *job_scope, job.lease_token)).fetchone()
                    if updated is None:
                        return
                    update_chunk = sql.SQL("""
                        UPDATE {} SET state = %s
                        WHERE profile_id = %s AND namespace = %s AND source_id = %s
                          AND chunk_id = %s AND content_hash = %s AND state = 'running'
                        RETURNING chunk_id
                    """).format(sql.Identifier(self.schema, "chunks"))
                    if conn.execute(update_chunk, (new_state, *scope)).fetchone() is None:
                        raise EmbeddingStorageError("失败处理时当前块版本不一致，修改已回滚")
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"保存嵌入任务失败状态失败（{type(exc).__name__}）") from None
    

    def list_ready(
        self, profile_id: str, namespace: str, source_id: str | None = None,
    ) -> list[tuple[IndexedChunk, list[float]]]:
        """用于调试/验收，返回当前 hash 对应的 ready 块及向量；正式查询用 search_ready。"""
        for name, value in (("profile_id", profile_id), ("namespace", namespace)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        if source_id is not None and (not isinstance(source_id, str) or not source_id.strip()):
            raise ValueError("source_id 必须是非空字符串或 None")
        try:
            with self.connect() as conn:
                query = sql.SQL("""
                    SELECT c.profile_id, c.namespace, c.source_id, c.chunk_id,
                           c.text, c.content_hash, c.metadata, c.state, v.embedding
                    FROM {} AS c
                    JOIN {} AS v
                      ON c.profile_id = v.profile_id AND c.content_hash = v.content_hash
                    WHERE c.profile_id = %s AND c.namespace = %s AND c.state = 'ready'
                """).format(sql.Identifier(self.schema, "chunks"), sql.Identifier(self.schema, "vectors"))
                params = [profile_id, namespace]
                if source_id is not None:
                    query += sql.SQL(" AND c.source_id = %s")
                    params.append(source_id)
                query += sql.SQL(" ORDER BY c.source_id, c.chunk_id")
                cursor = conn.execute(query, tuple(params))
                columns = [column.name for column in cursor.description or ()]
                expected = {"profile_id", "namespace", "source_id", "chunk_id", "text",
                            "content_hash", "metadata", "state", "embedding"}
                if len(columns) != len(expected) or set(columns) != expected:
                    raise EmbeddingStorageError("就绪块查询的返回列不完整或重复")
                result = []
                for row in cursor.fetchall():
                    if len(row) != len(columns):
                        raise EmbeddingStorageError("就绪块记录的字段数量与查询列不一致")
                    data = dict(zip(columns, row))
                    embedding = data.pop("embedding")
                    chunk = IndexedChunk(**data)
                    if (chunk.profile_id != profile_id or chunk.namespace != namespace
                            or (source_id is not None and chunk.source_id != source_id)
                            or chunk.state != "ready"):
                        raise EmbeddingStorageError("就绪块记录超出查询范围或未就绪")
                    if (any(not isinstance(value, str) or not value.strip()
                            for value in (chunk.source_id, chunk.chunk_id, chunk.text, chunk.content_hash))
                            or not isinstance(chunk.metadata, dict)):
                        raise EmbeddingStorageError("就绪块正文、身份或元数据损坏")
                    try:
                        if isinstance(embedding, (str, bytes, dict)):
                            raise ValueError
                        values = embedding.to_list() if isinstance(embedding, Vector) else list(embedding)
                        if not values or any(isinstance(value, bool) or not isinstance(value, Real) for value in values):
                            raise ValueError
                        vector = [float(value) for value in values]
                        if not all(math.isfinite(value) for value in vector) or not any(vector):
                            raise ValueError
                    except (TypeError, ValueError, OverflowError):
                        raise EmbeddingStorageError("就绪向量损坏：必须是非空、有限、非零的一维数值向量") from None
                    chunk.metadata = deepcopy(chunk.metadata)
                    result.append((chunk, vector))
            return result
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"读取就绪索引失败（{type(exc).__name__}）") from None

    def search_ready(
        self, profile_id: str, namespace: str, query_vector: list[float],
        limit: int = 5, source_id: str | None = None,
    ) -> list[EmbeddingHit]:
        """在数据库内筛选并排序，不把全部向量拉回 Python。

        联合 chunks/vectors，限定模型、namespace、当前 hash、ready 状态及可选来源；
        ORDER BY embedding <=> query_vector LIMIT ...，score 为 1-余弦距离。
        先做精确排序基线；HNSW 是后续可对照的近似索引，不能声称已启用。
        """
        for name, value in (("profile_id", profile_id), ("namespace", namespace)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        if source_id is not None and (not isinstance(source_id, str) or not source_id.strip()):
            raise ValueError("source_id 必须是非空字符串或 None")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        if (not isinstance(query_vector, list) or not query_vector
                or any(isinstance(value, bool) or not isinstance(value, Real) for value in query_vector)):
            raise ValueError("查询向量必须是非空的一维数值列表")
        try:
            values = [float(value) for value in query_vector]
            if any(not math.isfinite(value) or abs(value) > 3.4028234663852886e38 for value in values):
                raise ValueError
            query_vector_db = Vector(values)
            if not any(query_vector_db.to_list()):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError("查询向量必须能表示为有限、非零的 float32 向量") from None

        try:
            with self.connect() as conn:
                dimensions_query = sql.SQL("SELECT dimensions FROM {} WHERE profile_id = %s").format(
                    sql.Identifier(self.schema, "profiles"),
                )
                profile = conn.execute(dimensions_query, (profile_id,)).fetchone()
                if profile is None or profile[0] is None:
                    return []
                dimensions = profile[0]
                if type(dimensions) is not int or dimensions <= 0:
                    raise EmbeddingStorageError("profile 的维度记录损坏")
                if dimensions != len(values):
                    raise EmbeddingStorageError("查询向量维度与该 profile 已记录的维度不一致")
                query = sql.SQL("""
                    SELECT c.profile_id, c.namespace, c.source_id, c.chunk_id,
                           c.text, c.content_hash, c.metadata, c.state,
                           v.embedding <=> %s::vector AS distance
                    FROM {} AS c
                    JOIN {} AS v
                      ON c.profile_id = v.profile_id AND c.content_hash = v.content_hash
                    WHERE c.profile_id = %s AND c.namespace = %s AND c.state = 'ready'
                """).format(sql.Identifier(self.schema, "chunks"), sql.Identifier(self.schema, "vectors"))
                params = [query_vector_db, profile_id, namespace]
                if source_id is not None:
                    query += sql.SQL(" AND c.source_id = %s")
                    params.append(source_id)
                query += sql.SQL(" ORDER BY v.embedding <=> %s::vector ASC, c.source_id, c.chunk_id LIMIT %s")
                params.extend([query_vector_db, limit])
                cursor = conn.execute(query, tuple(params))
                columns = [column.name for column in cursor.description or ()]
                expected = {"profile_id", "namespace", "source_id", "chunk_id", "text",
                            "content_hash", "metadata", "state", "distance"}
                if len(columns) != len(expected) or set(columns) != expected:
                    raise EmbeddingStorageError("向量检索的返回列不完整或重复")
                hits = []
                for row in cursor.fetchall():
                    if len(row) != len(columns):
                        raise EmbeddingStorageError("向量检索记录的字段数量与查询列不一致")
                    data = dict(zip(columns, row))
                    distance = data.pop("distance")
                    chunk = IndexedChunk(**data)
                    if (chunk.profile_id != profile_id or chunk.namespace != namespace
                            or (source_id is not None and chunk.source_id != source_id)
                            or chunk.state != "ready"):
                        raise EmbeddingStorageError("检索记录超出查询范围或未就绪")
                    if (any(not isinstance(value, str) or not value.strip()
                            for value in (chunk.source_id, chunk.chunk_id, chunk.text, chunk.content_hash))
                            or not isinstance(chunk.metadata, dict)):
                        raise EmbeddingStorageError("检索记录的正文、身份或元数据损坏")
                    if isinstance(distance, bool) or not isinstance(distance, Real) or not math.isfinite(distance):
                        raise EmbeddingStorageError("检索距离不是有限数值")
                    chunk.metadata = deepcopy(chunk.metadata)
                    hits.append(EmbeddingHit(chunk=chunk, score=1.0 - float(distance)))
            return hits
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"查询就绪索引失败（{type(exc).__name__}）") from None

    def status(self, profile_id: str, namespace: str) -> IndexStatus:
        """统计当前块的四种状态；空库各项为零，查询失败抛出异常。"""
        if any(not isinstance(value, str) or not value.strip() for value in (profile_id, namespace)):
            raise ValueError("profile_id 和 namespace 必须是非空字符串")
        try:
            with self.connect() as conn:
                query = sql.SQL('''
                    SELECT state, COUNT(*)
                    FROM {}
                    WHERE profile_id = %s AND namespace = %s
                    GROUP BY state
                ''').format(sql.Identifier(self.schema, "chunks"))
                rows = conn.execute(query, (profile_id, namespace))
                counts = dict(rows)
                if any(state not in ("pending", "running", "ready", "failed")
                       or type(count) is not int or count < 0
                       for state, count in counts.items()):
                    raise EmbeddingStorageError("索引状态统计结果损坏")
                return IndexStatus(**counts)
        except EmbeddingStorageError:
            raise
        except Exception as exc:
            raise EmbeddingStorageError(f"读取索引状态失败（{type(exc).__name__}）") from None
