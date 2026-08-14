"""Minimal framework-neutral SSE agent implementing the AgentLens contract."""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="AgentLens scripted example agent")


class RunRequest(BaseModel):
    run_id: str
    task_id: str
    task_input: dict
    seed: int
    environment_snapshot: str
    tool_gateway_url: str
    budget: dict


def encode(run_id: str, seq: int, event_type: str, payload: dict) -> str:
    body = {"run_id": run_id, "seq": seq, "timestamp": datetime.now(timezone.utc).isoformat(), "type": event_type, "span_id": None, "parent_span_id": None, "payload": payload}
    return f"event: {event_type}\ndata: {json.dumps(body)}\n\n"


@app.post("/v1/agent-runs")
async def run_agent(request: RunRequest):
    async def events():
        yield encode(request.run_id, 0, "run.started", {"seed": request.seed})
        yield encode(request.run_id, 1, "tool.call", {"name": "search_customer", "arguments": request.task_input})
        yield encode(request.run_id, 2, "tool.result", {"name": "search_customer", "result": {"found": True}})
        yield encode(request.run_id, 3, "usage", {"input_tokens": 420, "output_tokens": 72, "cached_tokens": 0})
        yield encode(request.run_id, 4, "final", {"answer": "任务已完成", "verified": True})
        yield encode(request.run_id, 5, "run.completed", {"status": "success"})
        await asyncio.sleep(0)
    return StreamingResponse(events(), media_type="text/event-stream")

