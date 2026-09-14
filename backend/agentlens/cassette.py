from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CASSETTE_CONTENT_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CassetteMissError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_environment_snapshot(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or value != value.strip() or any(
        character.isspace() for character in value
    ):
        raise ValueError("environment snapshot must be <cassette-id>:<version>")
    parts = value.split(":")
    if len(parts) != 2 or not all(parts):
        raise ValueError("environment snapshot must be <cassette-id>:<version>")
    return parts[0], parts[1]


@dataclass
class ToolCassette:
    mode: str = "replay"
    records: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def key(tool: str, arguments: dict[str, Any]) -> str:
        return f"{tool}:{canonical_sha256(arguments)[:16]}"

    def content_sha256(self) -> str:
        return canonical_sha256({"mode": self.mode, "records": self.records})

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
