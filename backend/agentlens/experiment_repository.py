from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from agentlens.database_core import (
    as_utc,
    database_lock,
    init_database,
    session_factory,
)
from agentlens.models import (
    CandidateRecord,
    EventRecord,
    ExperimentRecord,
    RunRecord,
)
from agentlens.schemas import (
    ExperimentRequest,
    ExperimentSummary,
    TaskSpec,
)


def _save_candidate(session: Session, candidate) -> None:
    record = session.get(CandidateRecord, candidate.id)
    values = candidate.model_dump(mode="json")
    if record is None:
        session.add(
            CandidateRecord(
                id=candidate.id,
                name=candidate.name,
                version=candidate.version,
                fingerprint=values,
            )
        )
    else:
        record.name = candidate.name
        record.version = candidate.version
        record.fingerprint = values


def execution_payload(record: ExperimentRecord) -> dict[str, Any]:
    return record.metrics if isinstance(record.metrics, dict) else {}


def _validate_progress(completed: Any, total: Any) -> tuple[int, int]:
    if type(completed) is not int or type(total) is not int:
        raise ValueError("progress completed and total must be integers")
    if completed < 0 or total < 0 or completed > total:
        raise ValueError("progress must satisfy 0 <= completed <= total")
    return completed, total


def summary_from_record(record: ExperimentRecord) -> ExperimentSummary | None:
    raw = execution_payload(record).get("summary")
    if not isinstance(raw, dict):
        return None
    try:
        summary = ExperimentSummary.model_validate(raw)
    except (TypeError, ValueError):
        return None
    progress = execution_payload(record).get("progress", {})
    completed = int(progress.get("completed", summary.completed_runs))
    plan = summary.plan
    if record.status in {"queued", "running"}:
        plan = [
            step.model_copy(
                update={
                    "status": (
                        "completed"
                        if step.key in {"snapshot", "calibrate"}
                        and record.status == "running"
                        else "active"
                        if step.key == "trials" and record.status == "running"
                        else step.status
                    ),
                    "detail": (
                        f"{min(completed, summary.total_runs)} / {summary.total_runs}"
                        if step.key == "trials"
                        else step.detail
                    ),
                }
            )
            for step in plan
        ]
    return summary.model_copy(
        update={
            "status": record.status,
            "completed_runs": min(completed, summary.total_runs),
            "plan": plan,
        }
    )


def persist_queued_experiment(
    experiment: ExperimentSummary,
    request: ExperimentRequest,
    tasks: Iterable[TaskSpec],
    snapshot: dict[str, Any],
) -> None:
    task_snapshot = tuple(task.model_copy(deep=True) for task in tasks)
    task_ids = [task.id for task in task_snapshot]
    if not task_snapshot or any(not task_id.strip() for task_id in task_ids):
        raise ValueError("task snapshot must contain named tasks")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task snapshot IDs must be unique")
    candidate_count = 2 if request.baseline_candidate_id else 1
    total_work = len(task_snapshot) * request.repetitions * candidate_count
    if experiment.completed_runs != 0 or experiment.total_runs != total_work:
        raise ValueError("queued summary does not match task snapshot work")
    expected_tasks = [task.model_dump(mode="json") for task in task_snapshot]
    if snapshot.get("tasks") != expected_tasks:
        raise ValueError("frozen snapshot does not match queued task snapshot")
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        for candidate in (experiment.candidate, experiment.baseline):
            if candidate is not None:
                _save_candidate(session, candidate)
        if session.get(ExperimentRecord, experiment.id) is not None:
            raise ValueError(f"experiment {experiment.id} already exists")
        session.add(
            ExperimentRecord(
                id=experiment.id,
                status="queued",
                prompt=experiment.prompt,
                candidate_id=experiment.candidate.id,
                baseline_id=experiment.baseline.id if experiment.baseline else None,
                snapshot=deepcopy(snapshot),
                metrics={
                    "summary": experiment.model_dump(mode="json"),
                    "request": request.model_dump(mode="json"),
                    "progress": {"completed": 0, "total": total_work},
                    "heartbeat_at": None,
                    "error": None,
                },
                created_at=experiment.created_at,
            )
        )


def persist_experiment(
    experiment: ExperimentSummary,
    expected_statuses: Iterable[str] | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> bool:
    """Upsert a complete audit record, optionally guarded by current state."""
    init_database()
    allowed = set(expected_statuses) if expected_statuses is not None else None
    with database_lock, session_factory()() as session, session.begin():
        for candidate in (experiment.candidate, experiment.baseline):
            if candidate is not None:
                _save_candidate(session, candidate)
        record = session.get(ExperimentRecord, experiment.id, with_for_update=True)
        if record is None and allowed is not None:
            return False
        if record is not None and allowed is not None and record.status not in allowed:
            return False
        previous = execution_payload(record) if record is not None else {}
        progress_total = len(experiment.runs) + len(experiment.baseline_runs)
        payload = {
            "summary": experiment.model_dump(mode="json"),
            "request": previous.get("request"),
            "progress": {"completed": progress_total, "total": progress_total},
            "error": None,
        }
        if record is None:
            if snapshot is None:
                raise ValueError("new completed experiment requires a frozen snapshot")
            record = ExperimentRecord(
                id=experiment.id,
                status=experiment.status,
                prompt=experiment.prompt,
                candidate_id=experiment.candidate.id,
                baseline_id=experiment.baseline.id if experiment.baseline else None,
                snapshot=deepcopy(snapshot),
                metrics=payload,
                created_at=experiment.created_at,
            )
            session.add(record)
        else:
            record.status = experiment.status
            record.prompt = experiment.prompt
            record.candidate_id = experiment.candidate.id
            record.baseline_id = experiment.baseline.id if experiment.baseline else None
            record.metrics = payload
            record.runs.clear()
            session.flush()
        for run in (*experiment.runs, *experiment.baseline_runs):
            run_record = RunRecord(
                id=run.run_id,
                experiment_id=experiment.id,
                task_id=run.task_id,
                candidate_id=run.candidate_id,
                seed=run.seed,
                status="completed",
                success=int(run.success),
                trajectory_score=run.trajectory_score,
                cost_cny=run.cost_cny,
                result=run.model_dump(mode="json", exclude={"events"}),
            )
            session.add(run_record)
            session.add_all(
                EventRecord(
                    run_id=run.run_id,
                    seq=event.seq,
                    type=event.type.value,
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    payload=event.payload,
                    timestamp=event.timestamp,
                )
                for event in run.events
            )
        return True


def load_experiment(experiment_id: str) -> ExperimentSummary | None:
    init_database()
    with database_lock, session_factory()() as session:
        record = session.get(ExperimentRecord, experiment_id)
        return summary_from_record(record) if record is not None else None


def list_persisted_experiments() -> list[ExperimentSummary]:
    init_database()
    with database_lock, session_factory()() as session:
        records = (
            session.query(ExperimentRecord)
            .order_by(ExperimentRecord.created_at.desc(), ExperimentRecord.id.desc())
            .all()
        )
        return [summary for record in records if (summary := summary_from_record(record))]


def latest_persisted_experiment() -> ExperimentSummary | None:
    items = list_persisted_experiments()
    return items[0] if items else None


def load_execution_state(experiment_id: str) -> dict[str, Any] | None:
    init_database()
    with database_lock, session_factory()() as session:
        record = session.get(ExperimentRecord, experiment_id)
        if record is None:
            return None
        payload = execution_payload(record)
        return {
            "status": record.status,
            "prompt": record.prompt,
            "progress": payload.get("progress", {"completed": 0, "total": 0}),
            "request": payload.get("request"),
            "snapshot": record.snapshot,
            "error": payload.get("error"),
            "experiment": summary_from_record(record),
        }


def compare_and_set_status(
    experiment_id: str,
    expected_statuses: Iterable[str],
    status: str,
) -> bool:
    init_database()
    expected = set(expected_statuses)
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None or record.status not in expected:
            return False
        record.status = status
        return True


def claim_experiment(experiment_id: str, now: datetime | None = None) -> bool:
    init_database()
    heartbeat_at = as_utc(now or datetime.now(UTC))
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None or record.status != "queued":
            return False
        payload = dict(execution_payload(record))
        payload["heartbeat_at"] = heartbeat_at.isoformat()
        record.metrics = payload
        record.status = "running"
        return True


def update_experiment_progress(
    experiment_id: str,
    completed: int,
    total: int,
    now: datetime | None = None,
) -> bool:
    completed, total = _validate_progress(completed, total)
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None or record.status != "running":
            return False
        payload = dict(execution_payload(record))
        stored_progress = payload.get("progress")
        if not isinstance(stored_progress, dict):
            raise TypeError("persisted progress is missing or invalid")
        previous_completed, expected_total = _validate_progress(
            stored_progress.get("completed"),
            stored_progress.get("total"),
        )
        if total != expected_total:
            raise ValueError("progress total cannot change during an experiment")
        if completed < previous_completed:
            raise ValueError("progress completed cannot decrease")
        payload["progress"] = {"completed": completed, "total": total}
        payload["heartbeat_at"] = as_utc(now or datetime.now(UTC)).isoformat()
        record.metrics = payload
        return True


def mark_stale_experiment(
    experiment_id: str,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> bool:
    init_database()
    current_time = as_utc(now or datetime.now(UTC))
    cutoff = current_time - timedelta(seconds=stale_after_seconds)
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None or record.status != "running":
            return False
        payload = dict(execution_payload(record))
        raw_heartbeat = payload.get("heartbeat_at")
        try:
            heartbeat_at = datetime.fromisoformat(raw_heartbeat)
        except (TypeError, ValueError):
            heartbeat_at = record.created_at
        if as_utc(heartbeat_at) > cutoff:
            return False
        payload["error"] = {
            "category": "worker_lost",
            "message": "experiment worker heartbeat expired",
        }
        record.metrics = payload
        record.status = "failed"
        return True


def mark_stale_experiments(stale_after_seconds: int) -> list[str]:
    init_database()
    with database_lock, session_factory()() as session:
        experiment_ids = [
            item[0]
            for item in session.query(ExperimentRecord.id)
            .filter(ExperimentRecord.status == "running")
            .all()
        ]
    return [
        experiment_id
        for experiment_id in experiment_ids
        if mark_stale_experiment(experiment_id, stale_after_seconds)
    ]


def mark_experiment_cancelled(experiment_id: str) -> bool:
    return compare_and_set_status(experiment_id, {"queued", "running"}, "cancelled")


def mark_experiment_failed(
    experiment_id: str,
    category: str = "internal_error",
    message: str = "experiment execution failed",
) -> bool:
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None or record.status not in {"queued", "running"}:
            return False
        payload = dict(execution_payload(record))
        payload["error"] = {"category": category, "message": message}
        record.metrics = payload
        record.status = "failed"
        return True


def persisted_counts() -> dict[str, int]:
    init_database()
    with database_lock, session_factory()() as session:
        return {
            "experiments": session.query(ExperimentRecord).count(),
            "runs": session.query(RunRecord).count(),
            "events": session.query(EventRecord).count(),
        }


def update_experiment_status(experiment_id: str, status: str) -> None:
    """Backward-compatible state update used by older integrations."""
    compare_and_set_status(
        experiment_id,
        {"queued", "running", "completed", "cancelled", "failed"},
        status,
    )
