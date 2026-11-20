"""E11–E15：可供 Memory/RAG 共用的增量嵌入练习，不在导入时请求网络。"""

from .client import EmbeddingClient, HTTPEmbeddingClient
from .config import EmbeddingConfig
from .indexer import IncrementalEmbeddingIndex
from .store import PostgresEmbeddingStore
from .types import EmbeddingChunk, EmbeddingHit, EmbeddingJob, IndexedChunk, IndexStatus, ProcessReport, SyncReport

__all__ = [
    "EmbeddingClient", "HTTPEmbeddingClient", "EmbeddingConfig",
    "IncrementalEmbeddingIndex", "PostgresEmbeddingStore", "EmbeddingChunk",
    "EmbeddingHit", "EmbeddingJob", "IndexedChunk", "IndexStatus", "ProcessReport", "SyncReport",
]
