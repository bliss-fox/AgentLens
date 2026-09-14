from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentlens.cassette import (
    CASSETTE_CONTENT_SHA256_PATTERN,
    CassetteMissError,
    ToolCassette,
    parse_environment_snapshot,
)
from agentlens.schemas import TOOL_GRANT_TOKEN_PATTERN


class ToolInvocation(BaseModel):
    run_id: str = Field(min_length=1, max_length=120)
    tool_grant_token: str = Field(pattern=TOOL_GRANT_TOKEN_PATTERN)
    cassette_id: str = "customer-tools-v2"
    cassette_content_sha256: str = Field(pattern=CASSETTE_CONTENT_SHA256_PATTERN)
    mode: Literal["replay"] = "replay"
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CassetteRegistry:
    def __init__(self) -> None:
        self._cassettes: dict[str, ToolCassette] = {}
        cassette = ToolCassette(mode="record")
        fixtures = [
            ("search_customer", {"customer_id": "C-1042"}, {"id": "C-1042", "name": "林晓"}),
            ("get_order", {"order_id": "O-8891"}, {"id": "O-8891", "status": "paid"}),
            ("check_refund_policy", {"order_id": "O-8891"}, {"eligible": True}),
        ]
        for tool, arguments, response in fixtures:
            cassette.invoke(tool, arguments, lambda response=response: response)
        cassette.mode = "replay"
        self._cassettes["customer-tools-v2"] = cassette
        # Deterministic coding tools execute in a pinned constrained-runner image.
        # The empty cassette is still hashed and frozen into experiment snapshots.
        self._cassettes["coding-agent-v1"] = ToolCassette(mode="replay")

    def invoke(self, invocation: ToolInvocation) -> dict[str, Any]:
        try:
            cassette = self._cassettes[invocation.cassette_id]
        except KeyError as error:
            raise CassetteMissError(f"unknown frozen cassette: {invocation.cassette_id}") from error
        if cassette.content_sha256() != invocation.cassette_content_sha256:
            raise CassetteMissError("frozen cassette content does not match requested digest")
        return cassette.invoke(invocation.tool, invocation.arguments)

    def snapshot(self, cassette_id: str) -> dict[str, Any]:
        cassette = self._cassettes[cassette_id]
        return {
            "id": cassette_id,
            "mode": cassette.mode,
            "entries": len(cassette.records),
            "keys": sorted(cassette.records),
            "content_sha256": cassette.content_sha256(),
        }

    def environment_contract(self, environment_snapshot: str) -> dict[str, Any]:
        cassette_id, version = parse_environment_snapshot(environment_snapshot)
        return {
            "environment_snapshot": environment_snapshot,
            "environment_snapshot_version": version,
            "cassette": self.snapshot(cassette_id),
        }


@lru_cache
def get_default_registry() -> CassetteRegistry:
    return CassetteRegistry()


class _LazyCassetteRegistry:
    def __getattr__(self, name: str):
        return getattr(get_default_registry(), name)


# Backward-compatible proxy. The fixture registry is built only on first use.
registry = _LazyCassetteRegistry()
