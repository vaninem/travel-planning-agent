"""共享数据契约。先读这个文件，再阅读工具和 ContextBuilder。"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Message:
    role: str
    content: str
    timestamp: datetime = field(default_factory=utc_now)


def history_prefix_hash(messages: list[Message]) -> str:
    """对按顺序排列的原始消息计算稳定的 UTC 指纹。"""
    payload = []
    for message in messages:
        if (not isinstance(message, Message) or not isinstance(message.role, str)
                or not message.role.strip() or not isinstance(message.content, str)
                or not isinstance(message.timestamp, datetime) or message.timestamp.utcoffset() is None):
            raise ValueError("历史消息必须有角色、正文与带时区时间")
        payload.append({
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp.astimezone(timezone.utc).isoformat(),
        })
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ContextUsage:
    """C01：裁剪前的预计输入量与可压缩历史范围 [history_start, history_end)。"""
    estimated_tokens: int
    input_budget: int
    trigger_tokens: int
    should_summarize: bool
    history_start: int = 0
    history_end: int = 0
    reason: str = ""


@dataclass
class ContextCompactionState:
    """C04：原始 history 保留；只在上下文视图中替换已摘要前缀。"""
    history_id: str = field(default_factory=lambda: f"history_{uuid4().hex}")
    task_id: str | None = None
    covered_until: int = 0
    prefix_hash: str | None = None
    summary_id: str | None = None


@dataclass
class HistorySummaryRequest:
    """C03：将 Message 区间转成有版本引用的 MemoryRecord，不结束当前任务。

    messages 是原始 history[start_index:end_index] 的副本；prefix_hash 是
    原始 history[:end_index] 的稳定指纹，用于复用检查与会话恢复。
    """
    history_id: str
    task_id: str
    start_index: int
    end_index: int
    messages: list[Message]
    prefix_hash: str
    previous_summary_id: str | None = None
    target_tokens: int | None = None


@dataclass
class ContextPacket:
    content: str
    timestamp: datetime = field(default_factory=utc_now)
    token_count: int = 0
    relevance_score: float | None = None  # None 表示还没计算，不用 0.5 作哨兵。
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Note:
    id: str
    title: str
    content: str
    note_type: str = "general"
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class DocumentChunk:
    id: str
    source: str
    content: str
    heading_path: str | None = None
    start: int = 0
    end: int = 0  # 原文的半开区间 [start, end)，不是字节偏移。
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    chunk: DocumentChunk
    score: float
    lexical_score: float | None = None
    vector_score: float | None = None
    rerank_score: float | None = None
    ranks: dict[str, int] = field(default_factory=dict)
    context_chunk: DocumentChunk | None = None
    fusion_score: float | None = None


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextBuildResult:
    context: str
    candidates: list[ContextPacket]
    selected: list[ContextPacket]
    token_count: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class TraceEvent:
    stage: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)


class TestRunDetails(TypedDict, total=False):
    error_kind: str
    elapsed_seconds: float


class TestRun(TestRunDetails):
    status: Literal["passed", "failed", "timeout", "sandbox_error"]
    exit_code: int | None
    output: str


class RepairDetails(TypedDict, total=False):
    changed_files: list[dict[str, Any]]
    phase: str
    verification: dict[str, TestRun]
    base_digest: str
    base_commit: str | None
    patch_recounted: bool
    error_kind: str
    submission_error: dict[str, str]
    applied_to: str
    apply_error: str


class RepairResult(RepairDetails):
    status: Literal["passed", "failed", "timeout", "patch_rejected", "sandbox_error", "not_reproduced", "regression_failed"]
    patch: str
    exit_code: int | None
    output: str


class ProjectResult(TypedDict):
    status: Literal["generated", "failed", "delivery_conflict"]
    target: str
    files: list[str]
    verification: TestRun | None


@dataclass
class AgentReply:
    answer: str
    context_result: ContextBuildResult
    trace: list[TraceEvent] = field(default_factory=list)
    repair: RepairResult | None = None
    baseline: dict[str, TestRun] | None = None
    project: ProjectResult | None = None
