from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from agentlens.application_dependencies import experiment_store_for
from agentlens.asset_repository import (
    get_benchmark,
    list_benchmarks,
    list_candidates,
)
from agentlens.config import BENCHMARK_ID, get_settings
from agentlens.demo import (
    CANDIDATES,
    TASKS,
    calibration_report,
    candidate_catalog,
)
from agentlens.experiment_repository import mark_experiment_failed
from agentlens.runtime import resolve_candidate
from agentlens.schemas import ExperimentRequest

router = APIRouter(tags=["experiments"])


@router.get("/api/v1/bootstrap")
def bootstrap(request: Request) -> dict:
    experiment_store = experiment_store_for(request.app)
    try:
        experiment = experiment_store.latest()
    except LookupError:
        experiment = None
    benchmarks = list_benchmarks()
    candidates = list_candidates()
    return {
        "experiment": (
            experiment.model_dump(mode="json") if experiment is not None else None
        ),
        "candidates": [x.model_dump(mode="json") for x in candidates],
        "benchmarks": [x.model_dump(mode="json") for x in benchmarks],
        "tasks": (
            [x.model_dump(mode="json") for x in benchmarks[0].tasks]
            if benchmarks
            else []
        ),
        "calibration": calibration_report(),
    }


@router.get("/api/v1/experiments")
def list_experiments(request: Request) -> list[dict]:
    experiment_store = experiment_store_for(request.app)
    return [x.model_dump(mode="json") for x in experiment_store.list()]


@router.get("/api/v1/experiments/{experiment_id}")
def get_experiment(experiment_id: str, request: Request) -> dict:
    experiment_store = experiment_store_for(request.app)
    experiment = experiment_store.get(experiment_id)
    if experiment is None:
        raise HTTPException(404, "experiment not found")
    return experiment.model_dump(mode="json")


@router.post("/api/v1/experiments", status_code=status.HTTP_202_ACCEPTED)
async def create_experiment(request: ExperimentRequest, http_request: Request) -> dict:
    experiment_store = experiment_store_for(http_request.app)
    candidates = (
        {item.id: item for item in list_candidates()}
        if request.benchmark_id != BENCHMARK_ID
        else candidate_catalog()
        if request.execution_mode == "http"
        else CANDIDATES
    )
    unknown = [
        x
        for x in (request.candidate_id, request.baseline_candidate_id)
        if x is not None and x not in candidates
    ]
    if unknown:
        raise HTTPException(422, {"category": "invalid_candidate", "ids": unknown})
    if request.benchmark_id != BENCHMARK_ID and get_benchmark(request.benchmark_id) is None:
        raise HTTPException(422, {"category": "invalid_benchmark", "id": request.benchmark_id})
    if request.execution_mode == "http":
        try:
            candidate = candidates[request.candidate_id]
            if candidate.model_parameters.get("identity_status") == "pending-readiness":
                raise ValueError(
                    "candidate runtime identity is unverified; run the connection test first"
                )
            resolve_candidate(candidate)
            if request.baseline_candidate_id:
                baseline = candidates[request.baseline_candidate_id]
                if baseline.model_parameters.get("identity_status") == "pending-readiness":
                    raise ValueError(
                        "baseline runtime identity is unverified; run its connection test first"
                    )
                resolve_candidate(baseline)
        except ValueError as error:
            raise HTTPException(
                422,
                {"category": "invalid_execution_config", "message": str(error)},
            ) from error
    try:
        experiment = await asyncio.to_thread(experiment_store.create_queued, request)
    except ValueError as error:
        raise HTTPException(
            422,
            {"category": "invalid_execution_config", "message": str(error)},
        ) from error
    if get_settings().execution_backend == "local":
        try:
            experiment_store.start_local(experiment.id)
        except Exception as error:
            await asyncio.to_thread(
                mark_experiment_failed,
                experiment.id,
                "executor_unavailable",
                "local experiment executor unavailable",
            )
            raise HTTPException(
                503,
                {
                    "category": "executor_unavailable",
                    "message": "local experiment executor unavailable",
                    "experiment_id": experiment.id,
                    "retryable": True,
                },
            ) from error
    else:
        try:
            job = await http_request.app.state.redis_pool.enqueue_job(
                "execute_experiment",
                experiment.id,
                _job_id=experiment.id,
            )
        except Exception as error:
            await asyncio.to_thread(
                mark_experiment_failed,
                experiment.id,
                "queue_unavailable",
                "experiment queue unavailable",
            )
            raise HTTPException(
                503,
                {
                    "category": "queue_unavailable",
                    "message": "experiment queue unavailable",
                    "experiment_id": experiment.id,
                    "retryable": True,
                },
            ) from error
        if job is None:
            await asyncio.to_thread(
                mark_experiment_failed,
                experiment.id,
                "queue_rejected",
                "experiment queue rejected",
            )
            raise HTTPException(
                503,
                {
                    "category": "queue_rejected",
                    "message": "experiment queue rejected",
                    "experiment_id": experiment.id,
                    "retryable": True,
                },
            )
    return experiment.model_dump(mode="json")


@router.post("/api/v1/experiments/{experiment_id}/cancel")
def cancel(experiment_id: str, request: Request) -> dict:
    experiment_store = experiment_store_for(request.app)
    if experiment_store.get(experiment_id) is None:
        raise HTTPException(404, "experiment not found")
    experiment = experiment_store.cancel(experiment_id)
    if experiment is None:
        raise HTTPException(409, "experiment is already terminal")
    return experiment.model_dump(mode="json")


@router.get("/api/v1/experiments/{experiment_id}/events")
async def experiment_events(experiment_id: str, request: Request) -> StreamingResponse:
    experiment_store = experiment_store_for(request.app)
    if await asyncio.to_thread(experiment_store.get, experiment_id) is None:
        raise HTTPException(404, "experiment not found")

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in experiment_store.progress_events(experiment_id, request.is_disconnected):
                yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:  # noqa: BLE001
            event = {
                "type": "experiment.error",
                "experiment_id": experiment_id,
                "category": "internal_error",
                "message": "experiment event stream failed",
                "status": "failed",
            }
            yield f"event: experiment.error\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.get("/api/v1/tasks")
def tasks(benchmark_id: str = "coding-agent-core-v1") -> list[dict]:
    benchmark = get_benchmark(benchmark_id)
    if benchmark is not None:
        return [x.model_dump(mode="json") for x in benchmark.tasks]
    if benchmark_id == BENCHMARK_ID:
        return [x.model_dump(mode="json") for x in TASKS]
    raise HTTPException(404, "benchmark not found")


@router.get("/api/v1/calibration")
def calibration() -> dict:
    return calibration_report()
