"""Memory 升级的数据契约；构造这些对象不会读取文件或调用模型。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..types import Message, utc_now

MemoryLayer = Literal["working", "summary", "archive"]
SearchMode = Literal["lexical", "vector", "hybrid"]
MemoryOperation = Literal["add", "update", "revise", "noop"]


@dataclass
class MemorySourceRef:
    memory_id: str
    version: int


@dataclass
class MemoryRecord:
    """唯一的业务记忆模型；created_at/updated_at 与事实有效期分别记录。

    layer 表示组织层次，memory_type 表示内容用途，两者独立。
    有效区间为 [valid_from, valid_to)，None 表示该方向不设界限。
    as_of 询问事实何时有效，不是“系统当时知道什么”的完整双时态查询。
    """
    id: str
    content: str
    user_id: str = "default"
    memory_type: str = "episodic"
    importance: float = 0.5
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    layer: MemoryLayer = "archive"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    task_id: str | None = None
    version: int = 1
    fact_key: str | None = None
    supersedes_id: str | None = None
    source_refs: list[MemorySourceRef] = field(default_factory=list)
    summary_state: Literal["fresh", "stale"] = "fresh"


@dataclass
class MemoryIndexEvent:
    """JSON→向量索引的待同步事件，不是 SQL jobs 表里的嵌入生成任务。

    不携带旧正文；重放时重新读取当前业务记录，删除后不能被旧事件复活。
    """
    id: str
    user_id: str
    memory_id: str
    operation: Literal["upsert", "delete"]
    version: int
    created_at: datetime = field(default_factory=utc_now)
    attempts: int = 0
    error_code: str | None = None


@dataclass
class MemorySnapshot:
    revision: int = 0
    records: list[MemoryRecord] = field(default_factory=list)
    index_events: list[MemoryIndexEvent] = field(default_factory=list)


@dataclass
class MemoryQuery:
    """查询条件没有 user_id；用户归属由服务实例绑定。"""
    as_of: datetime | None = None
    layers: tuple[MemoryLayer, ...] | None = None
    task_id: str | None = None
    memory_type: str | None = None
    min_importance: float | None = None
    max_importance: float | None = None


@dataclass
class MemoryHit:
    memory: MemoryRecord
    score: float
    lexical_score: float | None = None
    vector_score: float | None = None
    ranks: dict[str, int] = field(default_factory=dict)


@dataclass
class  MemoryContext:
    working: list[MemoryRecord] = field(default_factory=list)
    summaries: list[MemoryRecord] = field(default_factory=list)
    facts: list[MemoryRecord] = field(default_factory=list)
    archive: list[MemoryRecord] = field(default_factory=list)
    token_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        """已提供的固定输出格式；M06 选择时要计算这些标签和时间的开销。"""
        sections = (("Current task", self.working), ("Historical task summaries", self.summaries),
                    ("Valid facts", self.facts), ("Original evidence", self.archive))
        rendered = []
        for title, records in sections:
            parts = []
            for record in records:
                start = record.valid_from.isoformat() if record.valid_from is not None else "-inf"
                end = record.valid_to.isoformat() if record.valid_to is not None else "+inf"
                parts.append(f"[id={record.id}; recorded={record.created_at.isoformat()}; valid=[{start},{end})]\n{record.content}")
            rendered.append(f"## {title}\n" + "\n\n".join(parts))
        return "\n\n".join(rendered)


@dataclass
class MemoryMutation:
    """模型提出的写入建议；apply 前还需验证归属、证据和目标版本。"""
    operation: MemoryOperation
    content: str
    target_id: str | None = None
    expected_version: int | None = None
    fact_key: str | None = None
    effective_from: datetime | None = None
    evidence_ids: list[str] = field(default_factory=list)
    reason: str = ""
    evidence_messages: dict[str, Message] = field(default_factory=dict, repr=False)


@dataclass
class MemorySyncReport:
    """applied/pending 按来源数量统计，errors 为 memory_id 到短错误码。"""
    applied: int = 0
    pending: int = 0
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class MemoryCleanupPlan:
    """M08 清理预览；候选携带版本，提交前需按最新快照重新检查。"""
    user_id: str
    revision: int
    checked_at: datetime
    cutoff: datetime
    candidates: list[MemorySourceRef] = field(default_factory=list)
    protected: dict[str, str] = field(default_factory=dict)


@dataclass
class MemoryCleanupReport:
    """清理结果；预览和无候选时不提交，只有成功保存才填写 committed_revision。"""
    plan: MemoryCleanupPlan | None = None
    dry_run: bool = True
    deleted_ids: list[str] = field(default_factory=list)
    index_event_ids: list[str] = field(default_factory=list)
    committed_revision: int | None = None
