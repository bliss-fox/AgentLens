from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agentlens.cassette import ToolCassette


class ToolInvocation(BaseModel):
    cassette_id: str = "customer-tools-v2"
    mode: Literal["record", "replay"] = "replay"
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

    def invoke(self, invocation: ToolInvocation) -> dict[str, Any]:
        cassette = self._cassettes.setdefault(invocation.cassette_id, ToolCassette(mode=invocation.mode))
        cassette.mode = invocation.mode
        return cassette.invoke(invocation.tool, invocation.arguments)

    def snapshot(self, cassette_id: str) -> dict[str, Any]:
        cassette = self._cassettes[cassette_id]
        return {"id": cassette_id, "mode": cassette.mode, "entries": len(cassette.records), "keys": sorted(cassette.records)}


registry = CassetteRegistry()
