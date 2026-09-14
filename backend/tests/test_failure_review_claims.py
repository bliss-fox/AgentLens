from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from agentlens.database_core import engine, session_factory
from agentlens.exceptions import FailureReviewStateError
from agentlens.models import Base, EventRecord, ExperimentRecord
from agentlens.review_repository import (
    claim_failure_review,
    release_failure_review_claim,
)
from agentlens.store import ExperimentStore


@pytest.fixture
def review_target():
    if str(engine().url) != "sqlite+pysqlite:///:memory:":
        raise RuntimeError("tests may only reset the isolated in-memory database")
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    store = ExperimentStore(seed_demo=True)
    experiment = store.latest()
    run = next(
        item
        for item in (*experiment.runs, *experiment.baseline_runs)
        if item.failures
    )
    return experiment.id, run.run_id


def test_failure_review_claim_is_exclusive_and_releasable(review_target):
    experiment_id, run_id = review_target
    now = datetime(2026, 8, 23, tzinfo=UTC)
    claim = claim_failure_review(
        experiment_id,
        run_id,
        0,
        force=False,
        lease_seconds=60,
        now=now,
    )

    assert claim.failure.event_range
    assert claim.task_id
    assert claim.candidate_id
    assert claim.events
    assert all(
        claim.failure.event_range[0] <= event.seq <= claim.failure.event_range[1]
        for event in claim.events
    )

    with pytest.raises(FailureReviewStateError) as captured:
        claim_failure_review(
            experiment_id,
            run_id,
            0,
            force=True,
            lease_seconds=60,
            now=now,
        )

    assert captured.value.category == "review_in_progress"
    assert release_failure_review_claim(experiment_id, run_id, 0, "wrong") is False
    assert release_failure_review_claim(
        experiment_id, run_id, 0, claim.token
    ) is True


def test_expired_failure_review_claim_can_be_reclaimed(review_target):
    experiment_id, run_id = review_target
    now = datetime(2026, 8, 23, tzinfo=UTC)
    stale_claim = claim_failure_review(
        experiment_id,
        run_id,
        0,
        force=False,
        lease_seconds=1,
        now=now,
    )

    replacement = claim_failure_review(
        experiment_id,
        run_id,
        0,
        force=False,
        lease_seconds=60,
        now=now + timedelta(seconds=2),
    )

    assert replacement.token != stale_claim.token
    assert release_failure_review_claim(
        experiment_id, run_id, 0, stale_claim.token
    ) is False
    assert release_failure_review_claim(
        experiment_id, run_id, 0, replacement.token
    ) is True




def test_failure_review_claim_preserves_verified_span_lineage(review_target):
    experiment_id, run_id = review_target
    initial_claim = claim_failure_review(
        experiment_id,
        run_id,
        0,
        force=False,
        lease_seconds=60,
    )
    event_sequence = initial_claim.events[0].seq
    assert release_failure_review_claim(
        experiment_id, run_id, 0, initial_claim.token
    )

    with session_factory()() as session, session.begin():
        experiment_record = session.get(ExperimentRecord, experiment_id)
        assert experiment_record is not None
        metrics = deepcopy(experiment_record.metrics)
        summary = metrics["summary"]
        run_payload = next(
            item
            for item in (*summary["runs"], *summary["baseline_runs"])
            if item["run_id"] == run_id
        )
        event_payload = next(
            item for item in run_payload["events"] if item["seq"] == event_sequence
        )
        event_payload["span_id"] = "span-reviewed"
        event_payload["parent_span_id"] = "span-parent"
        experiment_record.metrics = metrics

        event_record = (
            session.query(EventRecord)
            .filter(
                EventRecord.run_id == run_id,
                EventRecord.seq == event_sequence,
            )
            .one()
        )
        event_record.span_id = "span-reviewed"
        event_record.parent_span_id = "span-parent"

    verified_claim = claim_failure_review(
        experiment_id,
        run_id,
        0,
        force=False,
        lease_seconds=60,
    )

    assert verified_claim.events[0].span_id == "span-reviewed"
    assert verified_claim.events[0].parent_span_id == "span-parent"
    assert release_failure_review_claim(
        experiment_id, run_id, 0, verified_claim.token
    )


def test_failure_review_claim_rejects_event_audit_mismatch(review_target):
    experiment_id, run_id = review_target
    claim = claim_failure_review(
        experiment_id,
        run_id,
        0,
        force=False,
        lease_seconds=60,
    )
    event_sequence = claim.events[0].seq
    assert release_failure_review_claim(
        experiment_id, run_id, 0, claim.token
    )

    with session_factory()() as session, session.begin():
        event_record = (
            session.query(EventRecord)
            .filter(
                EventRecord.run_id == run_id,
                EventRecord.seq == event_sequence,
            )
            .one()
        )
        event_record.payload = {**event_record.payload, "tampered": True}

    with pytest.raises(FailureReviewStateError) as captured:
        claim_failure_review(
            experiment_id,
            run_id,
            0,
            force=False,
            lease_seconds=60,
        )

    assert captured.value.category == "failure_context_unavailable"
    assert captured.value.public_message == "persisted event evidence is inconsistent"
