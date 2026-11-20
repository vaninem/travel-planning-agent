"""组件之间只依赖这些接口；以后可以替换数据库或模型。"""

from __future__ import annotations

from typing import Any, Protocol, TYPE_CHECKING
from .types import Message, SearchHit

if TYPE_CHECKING:
    from .memory.types import MemoryRecord


class ChatModel(Protocol):
    def generate(self, messages: list[Message]) -> str: ...


class MemoryRetriever(Protocol):
    def search(self, query: str, limit: int = 5, **filters: Any) -> list[MemoryRecord]: ...


class KnowledgeRetriever(Protocol):
    def search(self, query: str, limit: int = 5) -> list[SearchHit]: ...
