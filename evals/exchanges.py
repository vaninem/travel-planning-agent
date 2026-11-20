"""Capture provider/model exchanges and replay them without running I/O."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any

from travel_agent.domain import PlanningError, ProviderError


TOOL_OPERATIONS = frozenset({"search_poi", "search_hotels", "get_weather", "get_route", "search_guides", "quote_attractions", "ctrip_search"})
ERROR_TYPES = {"PlanningError": PlanningError, "ProviderError": ProviderError}


def _encode(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported trace field type {type(value).__name__}")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_encode)


def snapshot(value: Any) -> Any:
    return json.loads(canonical(value))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def serialized_error(error: ProviderError) -> dict:
    return {
        "type": "PlanningError" if isinstance(error, PlanningError) else "ProviderError",
        "code": error.code,
        "message": str(error),
        "retryable": error.retryable,
    }


class ReplayMismatch(RuntimeError):
    """A replay diverged in operation, arguments, stages, or final result."""


class ToolSnapshot:
    """Reuse normalized tool responses between strategies, keyed by exact arguments."""

    def __init__(self, responses: Iterable[dict] = (), *, strict: bool = False) -> None:
        self.responses: dict[tuple[str, str], dict] = {}
        self.strict = strict
        for entry in responses:
            op = entry["operation"]
            arguments = entry["arguments"]
            if op not in TOOL_OPERATIONS:
                raise ValueError(f"Unsupported snapshot operation {op!r}")
            self.responses[(op, canonical(arguments))] = snapshot(entry)

    def resolve(self, op: str, arguments: dict, perform: Callable[[], Any]) -> tuple[Any, str]:
        key = (op, canonical(arguments))
        if key in self.responses:
            return snapshot(self.responses[key]["result"]), "snapshot"
        if self.strict:
            # Surface missing fixtures even if the planner permits optional tool failures.
            raise ReplayMismatch(f"Missing {op} in tool snapshot ({digest(arguments)[:12]})")
        result = snapshot(perform())
        self.responses[key] = {"operation": op, "arguments": snapshot(arguments), "result": result}
        return snapshot(result), "provider"

    def entries(self) -> list[dict]:
        return [self.responses[key] for key in sorted(self.responses)]


class Recorder:
    def __init__(self, profile: str, tool_snapshot: ToolSnapshot) -> None:
        self.profile = profile
        self.tool_snapshot = tool_snapshot
        self.operations: list[dict] = []
        self.injected_faults = 0
        self.model_invocations = 0
        self.model_responses = 0

    def __call__(self, op: str, arguments: dict, perform: Callable[[], Any]) -> Any:
        entry = {"operation": op, "arguments": snapshot(arguments), "source": "provider"}
        self.operations.append(entry)
        try:
            if self.profile == "timeout" and op == "search_poi" and self.injected_faults == 0:
                entry["source"] = "injected"
                self.injected_faults += 1
                raise ProviderError("eval_timeout", "评测注入：地点查询超时", retryable=True)
            if op in TOOL_OPERATIONS:
                result, entry["source"] = self.tool_snapshot.resolve(op, arguments, perform)
            else:
                if op == "model":
                    self.model_invocations += 1
                result = perform()
        except ProviderError as error:
            if op == "model" and error.code == "model_config":
                self.model_invocations -= 1
            entry["error"] = serialized_error(error)
            raise
        if op == "model":
            self.model_responses += 1
        entry["result"] = snapshot(result)
        return snapshot(result)


class Replayer:
    def __init__(self, operations: list[dict]) -> None:
        self.operations = operations
        self.position = 0

    def __call__(self, op: str, arguments: dict, _perform: Callable[[], Any]) -> Any:
        if self.position >= len(self.operations):
            raise ReplayMismatch(f"Unexpected {op} after all exchanges were consumed")
        expected = self.operations[self.position]
        position = self.position
        self.position += 1
        if expected["operation"] != op or canonical(expected["arguments"]) != canonical(arguments):
            raise ReplayMismatch(f"Exchange {position} expected {expected['operation']}, got {op} or different arguments")
        if "error" in expected:
            error = expected["error"]
            raise ERROR_TYPES[error["type"]](error["code"], error["message"], retryable=error["retryable"])
        return snapshot(expected["result"])

    def finish(self) -> None:
        if self.position != len(self.operations):
            raise ReplayMismatch(f"Consumed {self.position} of {len(self.operations)} recorded exchanges")
