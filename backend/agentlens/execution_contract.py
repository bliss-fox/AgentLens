from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agentlens.calibration_cases import SEMANTIC_CALIBRATION_VERSION
from agentlens.config import (
    BENCHMARK_ID,
    CALIBRATION_VERSION,
    EVALUATOR_VERSION,
    PRICE_TABLE_VERSION,
    TASK_SET_VERSION,
)
from agentlens.schemas import (
    CandidateSpec,
    ExperimentRequest,
    ExperimentSummary,
    TaskSpec,
    normalize_http_url,
)


@dataclass(frozen=True)
class ExecutionContract:
    request: ExperimentRequest
    candidate: CandidateSpec
    baseline: CandidateSpec | None
    tasks: tuple[TaskSpec, ...]
    benchmark_name: str
    environment_snapshot: str
    cassette_content_sha256: str
    tool_gateway_url: str


def freeze_task_snapshot(tasks: Iterable[TaskSpec]) -> tuple[TaskSpec, ...]:
    frozen = tuple(task.model_copy(deep=True) for task in tasks)
    task_ids = [task.id for task in frozen]
    if not frozen or any(not task_id.strip() for task_id in task_ids):
        raise ValueError("task snapshot must contain named tasks")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task snapshot IDs must be unique")
    return frozen


def build_experiment_snapshot(
    experiment: ExperimentSummary,
    tasks: Iterable[TaskSpec],
    *,
    environment_snapshot: str,
    cassette_contract: dict[str, Any],
    request: ExperimentRequest | None = None,
    tool_gateway_url: str | None = None,
    benchmark_id: str = BENCHMARK_ID,
    task_set_version: str = TASK_SET_VERSION,
) -> dict[str, Any]:
    frozen_tasks = freeze_task_snapshot(tasks)
    calibration_version = (
        SEMANTIC_CALIBRATION_VERSION
        if request is not None and request.execution_mode == "http"
        else CALIBRATION_VERSION
    )
    snapshot: dict[str, Any] = {
        "benchmark_id": benchmark_id,
        "benchmark": experiment.benchmark_name,
        "task_set_version": task_set_version,
        "calibration": calibration_version,
        "tasks": [task.model_dump(mode="json") for task in frozen_tasks],
        "candidate": experiment.candidate.model_dump(mode="json"),
        "baseline": (
            experiment.baseline.model_dump(mode="json")
            if experiment.baseline is not None
            else None
        ),
        "cassette": environment_snapshot,
        "cassette_contract": deepcopy(cassette_contract),
        "price_table": PRICE_TABLE_VERSION,
        "evaluator": EVALUATOR_VERSION,
    }
    if request is not None:
        if tool_gateway_url is None:
            raise ValueError("queued experiment snapshot requires a tool gateway URL")
        snapshot["execution_mode"] = request.execution_mode
        snapshot["tool_gateway_url"] = normalize_http_url(
            tool_gateway_url,
            "tool gateway URL",
        )
    return snapshot


def validate_execution_contract(
    state: dict[str, Any],
    environment_contract_for: Callable[[str], dict[str, Any]],
) -> ExecutionContract:
    request = ExperimentRequest.model_validate(state["request"])
    snapshot = state["snapshot"]
    if not isinstance(snapshot, dict):
        raise TypeError("experiment snapshot must be an object")
    candidate = CandidateSpec.model_validate(snapshot["candidate"])
    raw_baseline = snapshot.get("baseline")
    baseline = CandidateSpec.model_validate(raw_baseline) if raw_baseline else None
    raw_tasks = snapshot["tasks"]
    if not isinstance(raw_tasks, list):
        raise TypeError("task snapshot must be a list")
    tasks = freeze_task_snapshot(TaskSpec.model_validate(task) for task in raw_tasks)

    progress = state["progress"]
    summary = state["experiment"]
    candidate_count = 2 if baseline is not None else 1
    expected_total = len(tasks) * request.repetitions * candidate_count
    if not isinstance(progress, dict):
        raise TypeError("persisted progress must be an object")
    if type(progress.get("total")) is not int:
        raise TypeError("persisted progress total must be an integer")
    if progress["total"] != expected_total:
        raise ValueError("task snapshot does not match persisted progress")
    if (
        summary is None
        or summary.total_runs != expected_total
        or summary.candidate != candidate
        or summary.baseline != baseline
        or snapshot.get("benchmark") != summary.benchmark_name
    ):
        raise ValueError("task snapshot does not match persisted summary")
    if request.prompt != state["prompt"]:
        raise ValueError("request prompt does not match the persisted record")
    if request.benchmark_id != snapshot.get("benchmark_id"):
        raise ValueError("benchmark snapshot does not match persisted request")

    expected_versions = {
        "calibration": (
            SEMANTIC_CALIBRATION_VERSION
            if request.execution_mode == "http"
            else CALIBRATION_VERSION
        ),
        "evaluator": EVALUATOR_VERSION,
        "price_table": PRICE_TABLE_VERSION,
    }
    if any(snapshot.get(key) != value for key, value in expected_versions.items()):
        raise ValueError("persisted evaluation contract is unsupported")
    if snapshot.get("execution_mode") != request.execution_mode:
        raise ValueError("execution mode does not match the persisted snapshot")
    if candidate.id != request.candidate_id:
        raise ValueError("candidate snapshot does not match the persisted request")
    if (baseline.id if baseline else None) != request.baseline_candidate_id:
        raise ValueError("baseline snapshot does not match the persisted request")
    if not isinstance(snapshot.get("benchmark"), str) or not isinstance(
        snapshot.get("task_set_version"), str
    ):
        raise TypeError("benchmark snapshot metadata is missing")

    environment_snapshot = snapshot["cassette"]
    cassette_contract = snapshot["cassette_contract"]
    tool_gateway_url = snapshot["tool_gateway_url"]
    if not isinstance(environment_snapshot, str) or not environment_snapshot:
        raise ValueError("environment snapshot is missing")
    if not isinstance(cassette_contract, dict):
        raise TypeError("cassette contract must be an object")
    if cassette_contract != environment_contract_for(environment_snapshot):
        raise ValueError("frozen cassette contract has drifted")
    cassette_snapshot = cassette_contract.get("cassette")
    if not isinstance(cassette_snapshot, dict):
        raise TypeError("cassette snapshot must be an object")
    cassette_content_sha256 = cassette_snapshot.get("content_sha256")
    if not isinstance(cassette_content_sha256, str):
        raise TypeError("cassette content digest must be a string")
    if not isinstance(tool_gateway_url, str) or not tool_gateway_url:
        raise ValueError("tool gateway URL is missing")

    return ExecutionContract(
        request=request,
        candidate=candidate,
        baseline=baseline,
        tasks=tasks,
        benchmark_name=snapshot["benchmark"],
        environment_snapshot=environment_snapshot,
        cassette_content_sha256=cassette_content_sha256,
        tool_gateway_url=normalize_http_url(tool_gateway_url, "tool gateway URL"),
    )
