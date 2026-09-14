from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from agentlens.asset_repository import (
    delete_benchmark,
    delete_candidate,
    get_candidate,
    list_benchmarks,
    list_candidates,
    save_benchmark,
    save_candidate,
    sync_candidate_identity,
)
from agentlens.schemas import BenchmarkSpec, CandidateSpec

router = APIRouter(prefix="/api/v1", tags=["assets"])


@router.get("/candidates")
def candidates() -> list[dict]:
    return [item.model_dump(mode="json") for item in list_candidates()]


@router.put("/candidates/{candidate_id}")
def put_candidate(candidate_id: str, body: CandidateSpec) -> dict:
    if candidate_id != body.id:
        raise HTTPException(422, "candidate id does not match path")
    return save_candidate(body).model_dump(mode="json")


@router.delete("/candidates/{candidate_id}", status_code=204)
def remove_candidate(candidate_id: str) -> None:
    try:
        if not delete_candidate(candidate_id):
            raise HTTPException(404, "candidate not found")
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/candidates/{candidate_id}/test")
async def test_candidate(candidate_id: str) -> dict:
    candidate = get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    if candidate.endpoint is None:
        raise HTTPException(422, "candidate endpoint is missing")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{candidate.endpoint}/ready")
        response.raise_for_status()
        readiness = response.json()
        if not isinstance(readiness, dict):
            raise TypeError("candidate readiness response must be an object")
        candidate = sync_candidate_identity(candidate, readiness)
        return {
            "ok": True,
            "status": readiness,
            "candidate": candidate.model_dump(mode="json"),
        }
    except (httpx.HTTPError, TypeError, ValueError) as error:
        raise HTTPException(
            424,
            {
                "category": "candidate_unavailable",
                "message": "candidate readiness check failed",
            },
        ) from error


@router.get("/benchmarks")
def benchmarks() -> list[dict]:
    return [item.model_dump(mode="json") for item in list_benchmarks()]


@router.put("/benchmarks/{benchmark_id}")
def put_benchmark(benchmark_id: str, body: BenchmarkSpec) -> dict:
    if benchmark_id != body.id:
        raise HTTPException(422, "benchmark id does not match path")
    return save_benchmark(body).model_dump(mode="json")


@router.delete("/benchmarks/{benchmark_id}", status_code=204)
def remove_benchmark(benchmark_id: str) -> None:
    if not delete_benchmark(benchmark_id):
        raise HTTPException(404, "benchmark not found")
