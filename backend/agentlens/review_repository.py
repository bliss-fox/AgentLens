from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from agentlens.database_core import (
    as_utc,
    database_lock,
    init_database,
    session_factory,
)
from agentlens.exceptions import FailureReviewStateError
from agentlens.experiment_repository import execution_payload, summary_from_record
from agentlens.models import EventRecord, ExperimentRecord, RunRecord
from agentlens.schemas import ExperimentSummary, FailureEvidence, TraceEvent


def _failure_identity(failure: FailureEvidence) -> tuple[Any, ...]:
    return (
        failure.category,
        failure.label,
        failure.severity,
        failure.event_range,
        failure.rule,
        failure.explanation,
    )


_FAILURE_REVIEW_CLAIMS_KEY = "failure_review_claims"


@dataclass(frozen=True)
class FailureReviewClaim:
    token: str
    failure: FailureEvidence
    task_id: str
    candidate_id: str
    events: tuple[TraceEvent, ...]


def _failure_review_claim_key(run_id: str, failure_index: int) -> str:
    return f"{run_id}:{failure_index}"


def _find_failure(
    summary: ExperimentSummary,
    run_id: str,
    failure_index: int,
) -> FailureEvidence:
    run = next(
        (item for item in (*summary.runs, *summary.baseline_runs) if item.run_id == run_id),
        None,
    )
    if run is None:
        raise LookupError("run not found")
    if failure_index < 0 or failure_index >= len(run.failures):
        raise LookupError("failure not found")
    return run.failures[failure_index]


def claim_failure_review(
    experiment_id: str,
    run_id: str,
    failure_index: int,
    *,
    force: bool,
    lease_seconds: float,
    now: datetime | None = None,
) -> FailureReviewClaim:
    """Reserve one paid review and return context verified against audit rows."""
    if lease_seconds <= 0:
        raise ValueError("failure review lease must be positive")
    init_database()
    claimed_at = as_utc(now or datetime.now(UTC))
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None:
            raise LookupError("experiment not found")
        if record.status != "completed":
            raise FailureReviewStateError(
                "review_not_ready",
                "only completed experiments can be reviewed",
                retryable=True,
            )
        summary = summary_from_record(record)
        if summary is None:
            raise FailureReviewStateError(
                "review_conflict",
                "completed experiment summary is unavailable",
                retryable=True,
            )
        failure = _find_failure(summary, run_id, failure_index)
        run_record = session.get(RunRecord, run_id, with_for_update=True)
        if (
            run_record is None
            or run_record.experiment_id != experiment_id
            or run_record.status != "completed"
        ):
            raise FailureReviewStateError(
                "failure_context_unavailable",
                "persisted run context is unavailable",
                retryable=False,
            )
        raw_result = run_record.result if isinstance(run_record.result, dict) else {}
        raw_failures = raw_result.get("failures")
        if not isinstance(raw_failures, list) or failure_index >= len(raw_failures):
            raise FailureReviewStateError(
                "failure_context_unavailable",
                "persisted run failure evidence is unavailable",
                retryable=False,
            )
        try:
            persisted_failure = FailureEvidence.model_validate(raw_failures[failure_index])
        except (TypeError, ValueError) as error:
            raise FailureReviewStateError(
                "failure_context_unavailable",
                "persisted run failure evidence is invalid",
                retryable=False,
            ) from error
        if persisted_failure != failure:
            raise FailureReviewStateError(
                "failure_context_unavailable",
                "persisted failure evidence is inconsistent",
                retryable=False,
            )
        target_run = next(
            (item for item in (*summary.runs, *summary.baseline_runs) if item.run_id == run_id),
            None,
        )
        if (
            target_run is None
            or target_run.task_id != run_record.task_id
            or target_run.candidate_id != run_record.candidate_id
            or target_run.seed != run_record.seed
        ):
            raise FailureReviewStateError(
                "failure_context_unavailable",
                "persisted run metadata is inconsistent",
                retryable=False,
            )
        start_sequence, end_sequence = failure.event_range
        event_records = (
            session.query(EventRecord)
            .filter(
                EventRecord.run_id == run_id,
                EventRecord.seq >= start_sequence,
                EventRecord.seq <= end_sequence,
            )
            .order_by(EventRecord.seq)
            .with_for_update()
            .all()
        )
        try:
            events = tuple(
                TraceEvent(
                    run_id=run_id,
                    seq=event_record.seq,
                    timestamp=as_utc(event_record.timestamp),
                    type=event_record.type,
                    span_id=event_record.span_id,
                    parent_span_id=event_record.parent_span_id,
                    payload=event_record.payload,
                )
                for event_record in event_records
            )
        except (TypeError, ValueError) as error:
            raise FailureReviewStateError(
                "failure_context_unavailable",
                "persisted event evidence is invalid",
                retryable=False,
            ) from error
        summary_events = tuple(
            event for event in target_run.events if start_sequence <= event.seq <= end_sequence
        )
        if not events or events != summary_events:
            raise FailureReviewStateError(
                "failure_context_unavailable",
                "persisted event evidence is inconsistent",
                retryable=False,
            )
        payload = dict(execution_payload(record))
        raw_claims = payload.get(_FAILURE_REVIEW_CLAIMS_KEY)
        claims = dict(raw_claims) if isinstance(raw_claims, dict) else {}
        claim_key = _failure_review_claim_key(run_id, failure_index)
        existing_claim = claims.get(claim_key)
        if isinstance(existing_claim, dict):
            try:
                expires_at = as_utc(datetime.fromisoformat(existing_claim["expires_at"]))
            except (KeyError, TypeError, ValueError):
                expires_at = claimed_at
            if expires_at > claimed_at:
                raise FailureReviewStateError(
                    "review_in_progress",
                    "failure review is already in progress",
                    retryable=True,
                )
        if failure.judge_verdict != "not_run" and not force:
            raise FailureReviewStateError(
                "review_already_exists",
                "failure already has a semantic review",
                retryable=False,
            )
        token = uuid4().hex
        claims[claim_key] = {
            "token": token,
            "started_at": claimed_at.isoformat(),
            "expires_at": (claimed_at + timedelta(seconds=lease_seconds)).isoformat(),
        }
        payload[_FAILURE_REVIEW_CLAIMS_KEY] = claims
        record.metrics = payload
        return FailureReviewClaim(
            token=token,
            failure=persisted_failure,
            task_id=run_record.task_id,
            candidate_id=run_record.candidate_id,
            events=events,
        )


def release_failure_review_claim(
    experiment_id: str,
    run_id: str,
    failure_index: int,
    claim_token: str,
) -> bool:
    """Release only the matching claim; a stale caller cannot clear a successor."""
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None:
            return False
        payload = dict(execution_payload(record))
        raw_claims = payload.get(_FAILURE_REVIEW_CLAIMS_KEY)
        if not isinstance(raw_claims, dict):
            return False
        claims = dict(raw_claims)
        claim_key = _failure_review_claim_key(run_id, failure_index)
        claim = claims.get(claim_key)
        if not isinstance(claim, dict) or claim.get("token") != claim_token:
            return False
        claims.pop(claim_key)
        if claims:
            payload[_FAILURE_REVIEW_CLAIMS_KEY] = claims
        else:
            payload.pop(_FAILURE_REVIEW_CLAIMS_KEY, None)
        record.metrics = payload
        return True


def persist_failure_review(
    experiment_id: str,
    run_id: str,
    failure_index: int,
    reviewed_failure: FailureEvidence,
    claim_token: str,
) -> ExperimentSummary:
    """Atomically synchronize a validated review into both audit JSON documents."""
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id, with_for_update=True)
        if record is None:
            raise LookupError("experiment not found")
        if record.status != "completed":
            raise ValueError("only completed experiments can be reviewed")
        summary = summary_from_record(record)
        if summary is None:
            raise ValueError("completed experiment summary is unavailable")
        metrics = dict(execution_payload(record))
        raw_claims = metrics.get(_FAILURE_REVIEW_CLAIMS_KEY)
        claim_key = _failure_review_claim_key(run_id, failure_index)
        claim = raw_claims.get(claim_key) if isinstance(raw_claims, dict) else None
        if not isinstance(claim, dict) or claim.get("token") != claim_token:
            raise FailureReviewStateError(
                "review_claim_lost",
                "failure review claim is no longer active",
                retryable=True,
            )

        group_name = "runs"
        run_position = next(
            (index for index, run in enumerate(summary.runs) if run.run_id == run_id),
            None,
        )
        target_runs = summary.runs
        if run_position is None:
            group_name = "baseline_runs"
            run_position = next(
                (index for index, run in enumerate(summary.baseline_runs) if run.run_id == run_id),
                None,
            )
            target_runs = summary.baseline_runs
        if run_position is None:
            raise LookupError("run not found")

        target_run = target_runs[run_position]
        if failure_index < 0 or failure_index >= len(target_run.failures):
            raise LookupError("failure not found")
        original_failure = target_run.failures[failure_index]
        if _failure_identity(original_failure) != _failure_identity(reviewed_failure):
            raise ValueError("review cannot modify deterministic failure evidence")

        run_record = session.get(RunRecord, run_id, with_for_update=True)
        if (
            run_record is None
            or run_record.experiment_id != experiment_id
            or run_record.status != "completed"
        ):
            raise LookupError("completed run not found")
        raw_result = run_record.result if isinstance(run_record.result, dict) else {}
        raw_failures = raw_result.get("failures")
        if not isinstance(raw_failures, list) or failure_index >= len(raw_failures):
            raise ValueError("persisted run failure evidence is unavailable")
        persisted_failure = FailureEvidence.model_validate(raw_failures[failure_index])
        if persisted_failure != original_failure:
            raise ValueError("persisted failure evidence is inconsistent")

        updated_failures = list(target_run.failures)
        updated_failures[failure_index] = reviewed_failure
        updated_run = target_run.model_copy(update={"failures": updated_failures})
        updated_group = list(target_runs)
        updated_group[run_position] = updated_run
        updated_summary = summary.model_copy(update={group_name: updated_group})

        claims = dict(raw_claims)
        claims.pop(claim_key)
        if claims:
            metrics[_FAILURE_REVIEW_CLAIMS_KEY] = claims
        else:
            metrics.pop(_FAILURE_REVIEW_CLAIMS_KEY, None)
        metrics["summary"] = updated_summary.model_dump(mode="json")
        record.metrics = metrics
        updated_raw_failures = list(raw_failures)
        updated_raw_failures[failure_index] = reviewed_failure.model_dump(mode="json")
        run_record.result = {**raw_result, "failures": updated_raw_failures}
        return updated_summary
