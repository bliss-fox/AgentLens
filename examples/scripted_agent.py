"""Minimal external SSE agent implementing the AgentLens trace contract."""

import asyncio
import json
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="AgentLens scripted example agent")


class RunRequest(BaseModel):
    run_id: str
    task_id: str
    task_input: dict
    seed: int
    environment_snapshot: str
    cassette_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_grant_token: str = Field(pattern=r"^[A-Za-z0-9_-]{32,200}$")
    tool_gateway_url: str
    budget: dict


CASES = {
    "task-1": (
        [("search_customer", {"customer_id": "C-1042"})],
        {"customer": {"id": "C-1042", "name": "林晓"}},
        "已找到客户 C-1042。",
    ),
    "task-2": (
        [("get_order", {"order_id": "O-8891"}), ("check_refund_policy", {"order_id": "O-8891"})],
        {"eligible": True},
        "订单符合退款条件。",
    ),
    "task-3": (
        [("get_order", {"order_id": "O-7790"}), ("create_ticket", {"order_id": "O-7790"})],
        {"ticket": {"id": "T-2048", "status": "open"}},
        "售后工单 T-2048 已创建。",
    ),
    "task-4": (
        [
            ("get_order", {"order_id": "O-2207"}),
            ("update_address", {"order_id": "O-2207", "city": "上海"}),
        ],
        {"address": {"city": "上海"}},
        "收货地址已修改为上海。",
    ),
    "task-5": (
        [
            ("get_invoice", {"invoice_id": "I-5521"}),
            ("get_payment", {"invoice_id": "I-5521"}),
            ("verify_amount", {"invoice_id": "I-5521"}),
        ],
        {"verified": True, "difference": 12.5},
        "账单差额为 ¥12.50。",
    ),
    "task-6": (
        [
            ("search_customer", {"customer_id": "C-9910"}),
            ("classify_priority", {"customer_id": "C-9910"}),
            ("escalate_case", {"customer_id": "C-9910"}),
        ],
        {"escalation": {"level": "high", "id": "E-61"}},
        "投诉已升级为高优先级。",
    ),
}


def encode(run_id: str, seq: int, event_type: str, payload: dict) -> str:
    body = {
        "run_id": run_id,
        "seq": seq,
        "timestamp": datetime.now(UTC).isoformat(),
        "type": event_type,
        "payload": payload,
    }
    return f"event: trace\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


@app.post("/v1/agent-runs")
async def run_agent(request: RunRequest):
    async def events():
        calls, state, answer = CASES[request.task_id]
        seq = 0
        yield encode(request.run_id, seq, "run.started", {"seed": request.seed})
        seq += 1
        for name, arguments in calls:
            yield encode(
                request.run_id,
                seq,
                "tool.call",
                {"name": name, "arguments": arguments, "valid_args": True},
            )
            seq += 1
            yield encode(
                request.run_id,
                seq,
                "tool.result",
                {"name": name, "result": {"ok": True, "source": "scripted-example"}},
            )
            seq += 1
        yield encode(request.run_id, seq, "state", {"state": state})
        seq += 1
        yield encode(
            request.run_id,
            seq,
            "usage",
            {"input_tokens": 420, "output_tokens": 72, "cached_tokens": 0},
        )
        seq += 1
        yield encode(
            request.run_id,
            seq,
            "final",
            {"answer": answer, "verified": True, "declared_complete": True},
        )
        seq += 1
        yield encode(request.run_id, seq, "run.completed", {"status": "success"})
        await asyncio.sleep(0)

    return StreamingResponse(events(), media_type="text/event-stream")
