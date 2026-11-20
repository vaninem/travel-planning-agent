"""已提供的接口契约；每个可变默认值互相独立。"""

from dataclasses import dataclass, field
from typing import Any, Literal


IndexState = Literal["pending", "running", "ready", "failed"]


@dataclass
class EmbeddingChunk:
    """由 Memory/RAG 提交：id 是来源内的块标识，text 是实际编码内容。"""
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IndexedChunk:
    profile_id: str
    namespace: str
    source_id: str
    chunk_id: str
    text: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
    state: IndexState = "pending"


@dataclass
class EmbeddingJob:
    id: str
    chunk: IndexedChunk
    attempts: int = 0
    lease_token: str | None = None
    lease_expires_at: float | None = None


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    cache_hits: int = 0
    queued: int = 0


@dataclass
class ProcessReport:
    """一次 process_pending 的统计；错误数表示受影响任务数，不保证失败报告已写入。

    request_errors 统计失败的客户端批次调用，generated 统计成功返回的唯一正文向量。
    """
    claimed: int = 0
    cache_hits: int = 0
    generated: int = 0
    completed: int = 0
    stale: int = 0
    generation_errors: int = 0
    request_errors: int = 0


@dataclass
class IndexStatus:
    pending: int = 0
    running: int = 0
    ready: int = 0
    failed: int = 0


@dataclass
class EmbeddingHit:
    chunk: IndexedChunk
    score: float
