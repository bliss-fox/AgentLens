from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from threading import Lock, Thread, current_thread
from time import monotonic
from typing import Any
from uuid import uuid4

from agentlens import demo as demo_module
from agentlens.asset_repository import get_benchmark, get_candidate
from agentlens.config import (
    BENCHMARK_ID,
    BENCHMARK_NAME,
    EXPERIMENT_HEARTBEAT_TIMEOUT_SECONDS,
    get_settings,
)
from agentlens.demo import (
    TASKS,
    build_demo_experiment,
    build_pending_experiment,
)
from agentlens.exceptions import ExperimentCancelled
from agentlens.execution_contract import (
    build_experiment_snapshot,
    validate_execution_contract,
)
from agentlens.experiment_repository import (
    claim_experiment,
    latest_persisted_experiment,
    list_persisted_experiments,
    load_execution_state,
    load_experiment,
    mark_experiment_cancelled,
    mark_experiment_failed,
    mark_stale_experiment,
    persist_experiment,
    persist_queued_experiment,
    update_experiment_progress,
)
from agentlens.schemas import ExperimentRequest, ExperimentSummary
from agentlens.tool_gateway import CassetteRegistry, get_default_registry


class ExperimentStore:
    def __init__(
        self,
        *,
        seed_demo: bool = False,
        runner: Callable[..., ExperimentSummary] = build_demo_experiment,
        cassette_registry: CassetteRegistry | None = None,
    ) -> None:
        self._runner = runner
        self._cassette_registry = (
            cassette_registry
            if cassette_registry is not None
            else get_default_registry()
        )
        self._threads: dict[str, Thread] = {}
        self._thread_lock = Lock()
        if seed_demo and latest_persisted_experiment() is None:
            initial = build_demo_experiment(
                "exp-demo-0248",
                "评估客服 Agent v1.4，在客服工具任务集上每题运行 10 次。"
                "重点检查工具误用和过早完成，并与 v1.3 对比。",
            )
            settings = get_settings()
            snapshot = build_experiment_snapshot(
                initial,
                TASKS,
                environment_snapshot=settings.environment_snapshot,
                cassette_contract=self._cassette_registry.environment_contract(
                    settings.environment_snapshot
                ),
            )
            persist_experiment(initial, snapshot=snapshot)

    def create_queued(
        self,
        request: ExperimentRequest,
        experiment_id: str | None = None,
    ) -> ExperimentSummary:
        experiment_id = experiment_id or f"exp-{uuid4().hex[:8]}"
        if request.benchmark_id == BENCHMARK_ID:
            task_snapshot = tuple(task.model_copy(deep=True) for task in TASKS)
            candidates = demo_module.candidate_catalog()
            candidate = candidates.get(request.candidate_id)
            baseline = (
                candidates.get(request.baseline_candidate_id)
                if request.baseline_candidate_id
                else None
            )
            benchmark_name = BENCHMARK_NAME
            task_set_version = "customer-tools-v2@2026-08-12"
            environment_snapshot = get_settings().environment_snapshot
        else:
            benchmark = get_benchmark(request.benchmark_id)
            candidate = get_candidate(request.candidate_id)
            baseline = (
                get_candidate(request.baseline_candidate_id)
                if request.baseline_candidate_id
                else None
            )
            if benchmark is None:
                raise ValueError(f"unknown benchmark: {request.benchmark_id}")
            task_snapshot = tuple(
                task.model_copy(deep=True) for task in benchmark.tasks
            )
            benchmark_name = benchmark.name
            task_set_version = benchmark.version
            environment_snapshot = benchmark.environment_snapshot
        if candidate is None:
            raise ValueError(f"unknown candidate: {request.candidate_id}")
        if request.baseline_candidate_id and baseline is None:
            raise ValueError(
                f"unknown baseline candidate: {request.baseline_candidate_id}"
            )
        summary = build_pending_experiment(
            experiment_id,
            request.prompt,
            request.candidate_id,
            request.baseline_candidate_id,
            request.repetitions,
            request.execution_mode,
            task_snapshot=task_snapshot,
            candidate_snapshot=candidate,
            baseline_snapshot=baseline,
            benchmark_name=benchmark_name,
        )
        settings = get_settings()
        snapshot = build_experiment_snapshot(
            summary,
            task_snapshot,
            environment_snapshot=environment_snapshot,
            cassette_contract=self._cassette_registry.environment_contract(
                environment_snapshot
            ),
            request=request,
            tool_gateway_url=settings.tool_gateway_url,
            benchmark_id=request.benchmark_id,
            task_set_version=task_set_version,
        )
        persist_queued_experiment(summary, request, task_snapshot, snapshot)
        return summary

    def execute(
        self,
        experiment_id: str,
        _legacy_request: ExperimentRequest | None = None,
    ) -> ExperimentSummary | None:
        state = load_execution_state(experiment_id)
        if state is None:
            return None
        if state["status"] == "running":
            mark_stale_experiment(experiment_id, EXPERIMENT_HEARTBEAT_TIMEOUT_SECONDS)
            return self.get(experiment_id)
        if state["status"] != "queued":
            return self.get(experiment_id)
        try:
            contract = validate_execution_contract(
                state,
                self._cassette_registry.environment_contract,
            )
        except (KeyError, TypeError, ValueError):
            mark_experiment_failed(
                experiment_id,
                "invalid_execution_state",
                "persisted experiment state is invalid",
            )
            return self.get(experiment_id)

        request = contract.request
        if not claim_experiment(experiment_id):
            return self.get(experiment_id)

        def on_progress(completed: int, total: int) -> None:
            update_experiment_progress(experiment_id, completed, total)

        def is_cancelled() -> bool:
            # asyncio.to_thread cannot force-stop its worker. Any durable terminal state
            # therefore becomes the cooperative stop signal for the underlying runner.
            current = load_execution_state(experiment_id)
            return current is None or current["status"] != "running"

        try:
            result = self._runner(
                experiment_id,
                request.prompt,
                request.candidate_id,
                request.baseline_candidate_id,
                request.repetitions,
                execution_mode=request.execution_mode,
                candidate_snapshot=contract.candidate,
                baseline_snapshot=contract.baseline,
                environment_snapshot=contract.environment_snapshot,
                cassette_content_sha256=contract.cassette_content_sha256,
                tool_gateway_url=contract.tool_gateway_url,
                task_snapshot=contract.tasks,
                benchmark_name=contract.benchmark_name,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
            )
            persist_experiment(result, expected_statuses={"running"})
        except ExperimentCancelled:
            mark_experiment_cancelled(experiment_id)
        except Exception:  # noqa: BLE001 - persist a stable public error, never a traceback.
            mark_experiment_failed(experiment_id)
        return self.get(experiment_id)

    def start_local(
        self,
        experiment_id: str,
        _legacy_request: ExperimentRequest | None = None,
    ) -> Thread:
        def target() -> None:
            try:
                self.execute(experiment_id)
            finally:
                with self._thread_lock:
                    if self._threads.get(experiment_id) is current_thread():
                        self._threads.pop(experiment_id, None)

        thread = Thread(target=target, name=f"agentlens-{experiment_id}", daemon=True)
        with self._thread_lock:
            self._threads[experiment_id] = thread
        try:
            thread.start()
        except BaseException:
            with self._thread_lock:
                if self._threads.get(experiment_id) is thread:
                    self._threads.pop(experiment_id, None)
            raise
        return thread

    def shutdown(self, wait_seconds: float = 2.0) -> list[str]:
        """Cancel active local experiments and wait briefly for their daemon threads."""
        with self._thread_lock:
            active = list(self._threads.items())
        for experiment_id, _ in active:
            mark_experiment_cancelled(experiment_id)
        deadline = monotonic() + max(0.0, wait_seconds)
        for _, thread in active:
            thread.join(max(0.0, deadline - monotonic()))
        return [experiment_id for experiment_id, thread in active if thread.is_alive()]

    def get(self, experiment_id: str) -> ExperimentSummary | None:
        return load_experiment(experiment_id)

    def latest(self) -> ExperimentSummary:
        latest = latest_persisted_experiment()
        if latest is None:
            raise LookupError("no experiments have been persisted")
        return latest

    def list(self) -> list[ExperimentSummary]:
        return list_persisted_experiments()

    def cancel(self, experiment_id: str) -> ExperimentSummary | None:
        if not mark_experiment_cancelled(experiment_id):
            return None
        return self.get(experiment_id)

    async def progress_events(
        self,
        experiment_id: str,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        delay_seconds: float = 0.05,
    ) -> AsyncIterator[dict[str, Any]]:
        """Poll durable state and emit each progress change plus exactly one terminal event."""
        last_progress: tuple[str, int, int] | None = None
        last_recovery_check = 0.0
        while True:
            if is_disconnected is not None and await is_disconnected():
                return
            state = await asyncio.to_thread(load_execution_state, experiment_id)
            if state is None:
                return
            status = state["status"]
            loop_time = asyncio.get_running_loop().time()
            if status == "running" and loop_time - last_recovery_check >= 1:
                last_recovery_check = loop_time
                if await asyncio.to_thread(
                    mark_stale_experiment,
                    experiment_id,
                    EXPERIMENT_HEARTBEAT_TIMEOUT_SECONDS,
                ):
                    continue
            progress = state["progress"]
            completed = int(progress.get("completed", 0))
            total = int(progress.get("total", 0))
            marker = (status, completed, total)
            if status in {"queued", "running"}:
                if marker != last_progress:
                    yield {
                        "type": "experiment.progress",
                        "experiment_id": experiment_id,
                        "completed": completed,
                        "total": total,
                        "status": status,
                    }
                    last_progress = marker
            elif status == "completed":
                if marker != last_progress:
                    yield {
                        "type": "experiment.progress",
                        "experiment_id": experiment_id,
                        "completed": completed,
                        "total": total,
                        "status": status,
                    }
                experiment = state["experiment"]
                if experiment is None:
                    yield {
                        "type": "experiment.error",
                        "experiment_id": experiment_id,
                        "category": "storage_error",
                        "message": "completed experiment result is unavailable",
                        "status": "failed",
                    }
                else:
                    yield {
                        "type": "experiment.result",
                        "experiment": experiment.model_dump(mode="json"),
                    }
                return
            elif status == "cancelled":
                yield {
                    "type": "experiment.cancelled",
                    "experiment_id": experiment_id,
                    "completed": completed,
                    "total": total,
                    "status": status,
                }
                return
            elif status == "failed":
                error = state.get("error") or {}
                experiment = state["experiment"]
                yield {
                    "type": "experiment.error",
                    "experiment_id": experiment_id,
                    "category": error.get("category", "internal_error"),
                    "message": error.get("message", "experiment execution failed"),
                    "status": status,
                    "experiment": (
                        experiment.model_dump(mode="json") if experiment is not None else None
                    ),
                }
                return
            await asyncio.sleep(delay_seconds)
