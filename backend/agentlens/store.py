from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from threading import Lock
from uuid import uuid4

from agentlens.database import persist_experiment, update_experiment_status
from agentlens.demo import build_demo_experiment
from agentlens.schemas import ExperimentRequest, ExperimentSummary


class ExperimentStore:
    def __init__(self) -> None:
        self._items: dict[str, ExperimentSummary] = {}
        self._cancelled: set[str] = set()
        self._lock = Lock()
        initial = build_demo_experiment(
            "exp-demo-0248",
            "评估客服 Agent v1.4，在客服工具任务集上每题运行 10 次。重点检查工具误用和过早完成，并与 v1.3 对比。",
        )
        self._items[initial.id] = initial

    def create(self, request: ExperimentRequest) -> ExperimentSummary:
        experiment_id = f"exp-{uuid4().hex[:8]}"
        result = build_demo_experiment(
            experiment_id, request.prompt, request.candidate_id,
            request.baseline_candidate_id, request.repetitions,
        )
        with self._lock:
            self._items[experiment_id] = result
        persist_experiment(result)
        return result

    def get(self, experiment_id: str) -> ExperimentSummary | None:
        return self._items.get(experiment_id)

    def latest(self) -> ExperimentSummary:
        return max(self._items.values(), key=lambda item: item.created_at)

    def list(self) -> list[ExperimentSummary]:
        return sorted(self._items.values(), key=lambda item: item.created_at, reverse=True)

    def cancel(self, experiment_id: str) -> ExperimentSummary | None:
        item = self._items.get(experiment_id)
        if item:
            self._cancelled.add(experiment_id)
            updated = item.model_copy(update={"status": "cancelled"})
            self._items[experiment_id] = updated
            update_experiment_status(experiment_id, "cancelled")
            return updated
        return None

    async def progress_events(
        self,
        experiment_id: str,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        delay_seconds: float = 0.08,
    ) -> AsyncIterator[dict]:
        """Yield progress without blocking the event loop.

        A disconnected consumer ends the iterator immediately. Explicit experiment
        cancellation is different: it is an application event and therefore gets a
        typed terminal event that clients can render and test.
        """
        item = self._items[experiment_id]
        milestones = [0, 12, 24, 36, 48, item.total_runs]
        for completed in milestones:
            if is_disconnected is not None and await is_disconnected():
                return
            if experiment_id in self._cancelled:
                yield {
                    "type": "experiment.cancelled",
                    "experiment_id": experiment_id,
                    "completed": completed,
                    "total": item.total_runs,
                    "status": "cancelled",
                }
                return
            yield {
                "type": "experiment.progress",
                "experiment_id": experiment_id,
                "completed": completed,
                "total": item.total_runs,
                "status": "completed" if completed == item.total_runs else "running",
            }
            await asyncio.sleep(delay_seconds)
        if is_disconnected is not None and await is_disconnected():
            return
        yield {"type": "experiment.result", "experiment": item.model_dump(mode="json")}


store = ExperimentStore()
