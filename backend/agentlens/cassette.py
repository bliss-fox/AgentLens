from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentlens.evaluation import canonical_hash


class CassetteMissError(RuntimeError):
    pass


@dataclass
class ToolCassette:
    mode: str = "replay"
    records: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def key(tool: str, arguments: dict[str, Any]) -> str:
        return f"{tool}:{canonical_hash(arguments)}"

    def invoke(
        self, tool: str, arguments: dict[str, Any], live_call: Callable[[], dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        key = self.key(tool, arguments)
        if key in self.records:
            return self.records[key]
        if self.mode == "replay":
            raise CassetteMissError(f"frozen cassette has no response for {key}")
        if live_call is None:
            raise CassetteMissError("record mode requires a live tool implementation")
        response = live_call()
        self.records[key] = response
        return response
