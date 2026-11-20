"""所有工具共享 run({action: ..., ...}) 入口；具体逻辑留在各工具中。"""

from abc import ABC, abstractmethod
from typing import Any
from ..types import ToolResult


class BaseTool(ABC):
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    def run(self, parameters: dict[str, Any]) -> ToolResult:
        """非法 action/参数抛 ValueError，练习未完成时抛 ExercisePending。"""
