"""RAG 搜索和离线评测的数据契约。"""

from dataclasses import dataclass
from typing import Literal


DEFAULT_CHUNK_TOKENS = 512
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_PARENT_TOKENS = 2_048
DEFAULT_EVIDENCE_TOKENS = 4_096
TEXT_MODEL_CHAR_LIMIT = 8_192
CHUNK_CHARS_PER_TOKEN = 12
DEFAULT_INDEX_HEADER_CHARS = TEXT_MODEL_CHAR_LIMIT - DEFAULT_CHUNK_TOKENS * CHUNK_CHARS_PER_TOKEN - 1


@dataclass(frozen=True)
class RAGSearchOptions:
    """默认走本地词法检索，其他阶段按需开启。"""

    mode: Literal["lexical", "vector", "hybrid"] = "lexical"
    source: str | None = None
    path_prefix: str | None = None
    language: str | None = None
    candidate_limit: int = 20
    rewrite_query: bool = False
    hyde: bool = False
    rerank: bool = False
    expand_parents: bool = False
    follow_dependencies: bool = False
    rounds: int = 1
    max_context_tokens: int = DEFAULT_EVIDENCE_TOKENS

    def __post_init__(self) -> None:
        if self.mode not in ("lexical", "vector", "hybrid"):
            raise ValueError("不支持的 RAG 检索模式")
        for value in (self.source, self.path_prefix, self.language):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("检索范围必须是非空字符串")
        for value in (self.candidate_limit, self.rounds, self.max_context_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("候选数、轮数和上下文预算必须是正整数")
        for value in (self.rewrite_query, self.hyde, self.rerank, self.expand_parents, self.follow_dependencies):
            if type(value) is not bool:
                raise ValueError("RAG 功能开关必须为 bool")
        if self.hyde and self.mode == "lexical":
            raise ValueError("HyDE 需要向量或混合检索")


@dataclass(frozen=True)
class QueryPlan:
    lexical_queries: tuple[str, ...]
    vector_queries: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalCase:
    query: str
    expected_ids: frozenset[str]


@dataclass(frozen=True)
class RetrievalMetrics:
    cases: int
    recall_at_k: float
    mrr_at_k: float
    p95_ms: float


@dataclass(frozen=True)
class VectorIndexComparison:
    queries: int
    mean_overlap_at_k: float
    exact_p95_ms: float
    approximate_p95_ms: float
