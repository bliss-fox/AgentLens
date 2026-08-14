from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agentlens.cassette import CassetteMissError
from agentlens.database import persist_experiment, persisted_counts
from agentlens.demo import CANDIDATES, TASKS, calibration_report
from agentlens.orchestrator import cancel_experiment, evaluation_graph
from agentlens.schemas import ExperimentRequest
from agentlens.store import store
from agentlens.tool_gateway import ToolInvocation, registry


@asynccontextmanager
async def lifespan(_: FastAPI):
    persist_experiment(store.latest())
    yield

app = FastAPI(title="AgentLens API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": "offline-demo", "storage": persisted_counts()}


@app.get("/api/v1/bootstrap")
def bootstrap() -> dict:
    return {
        "experiment": store.latest().model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in CANDIDATES.values()],
        "tasks": [item.model_dump(mode="json") for item in TASKS],
        "calibration": calibration_report(),
    }


@app.get("/api/v1/experiments")
def list_experiments() -> list[dict]:
    return [item.model_dump(mode="json") for item in store.list()]


@app.get("/api/v1/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict:
    experiment = store.get(experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment.model_dump(mode="json")


@app.post("/api/v1/experiments")
async def create_experiment(request: ExperimentRequest) -> dict:
    if evaluation_graph:
        state = await evaluation_graph.ainvoke({"request": request})
        experiment = store.get(state["experiment_id"])
    else:
        experiment = store.create(request)
    assert experiment is not None
    return experiment.model_dump(mode="json")


@app.post("/api/v1/experiments/{experiment_id}/cancel")
def cancel(experiment_id: str) -> dict:
    experiment = cancel_experiment(experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment.model_dump(mode="json")


@app.get("/api/v1/experiments/{experiment_id}/events")
async def experiment_events(experiment_id: str, request: Request) -> StreamingResponse:
    if not store.get(experiment_id):
        raise HTTPException(status_code=404, detail="experiment not found")

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in store.progress_events(experiment_id, request.is_disconnected):
                yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:  # noqa: BLE001 - SSE must end with a typed error for any producer failure.
            # Keep the wire contract stable and avoid exposing internal tracebacks.
            event = {
                "type": "experiment.error",
                "experiment_id": experiment_id,
                "category": "internal_error",
                "message": "experiment event stream failed",
                "status": "failed",
            }
            yield f"event: experiment.error\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/tasks")
def tasks() -> list[dict]:
    return [item.model_dump(mode="json") for item in TASKS]


@app.get("/api/v1/calibration")
def calibration() -> dict:
    return calibration_report()


@app.post("/api/v1/tools/invoke")
def invoke_tool(invocation: ToolInvocation) -> dict:
    try:
        return {"result": registry.invoke(invocation), "source": "frozen_cassette"}
    except CassetteMissError as error:
        raise HTTPException(status_code=424, detail={"category": "environment_error", "message": str(error)}) from error


@app.get("/api/v1/cassettes/{cassette_id}")
def cassette_snapshot(cassette_id: str) -> dict:
    try:
        return registry.snapshot(cassette_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="cassette not found") from error
