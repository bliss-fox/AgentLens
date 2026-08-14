from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agentlens.config import get_settings
from agentlens.models import Base, CandidateRecord, EventRecord, ExperimentRecord, RunRecord
from agentlens.schemas import ExperimentSummary


@lru_cache
def engine():
    url = get_settings().database_url
    options = {"check_same_thread": False} if url.startswith("sqlite") else {}
    pool_options = {"poolclass": StaticPool} if ":memory:" in url else {}
    return create_engine(url, pool_pre_ping=True, connect_args=options, **pool_options)


@lru_cache
def session_factory():
    return sessionmaker(engine(), expire_on_commit=False)


def init_database() -> None:
    Base.metadata.create_all(engine())


def sessions() -> Iterator[Session]:
    with session_factory()() as session:
        yield session


def persist_experiment(experiment: ExperimentSummary) -> None:
    """Persist the complete auditable experiment, including events, in one transaction."""
    init_database()
    with session_factory()() as session, session.begin():
        for candidate in (experiment.candidate, experiment.baseline):
            if session.get(CandidateRecord, candidate.id) is None:
                session.add(CandidateRecord(
                    id=candidate.id, name=candidate.name, version=candidate.version,
                    fingerprint=candidate.model_dump(mode="json"),
                ))
        if session.get(ExperimentRecord, experiment.id) is not None:
            return
        record = ExperimentRecord(
            id=experiment.id, status=experiment.status, prompt=experiment.prompt,
            candidate_id=experiment.candidate.id, baseline_id=experiment.baseline.id,
            snapshot={
                "benchmark": experiment.benchmark_name,
                "candidate": experiment.candidate.model_dump(mode="json"),
                "baseline": experiment.baseline.model_dump(mode="json"),
                "cassette": "customer-tools-v2:cassette-2026-08-12",
                "price_table": "pricing-cny-2026-08",
                "evaluator": "agentlens-evaluator@0.1.0",
            },
            metrics={
                "metrics": experiment.metrics,
                "comparison": experiment.comparison,
                "calibration": experiment.judge_calibration,
                "failures": experiment.failures,
                "verdict": experiment.verdict,
            },
            created_at=experiment.created_at,
        )
        session.add(record)
        for run in experiment.runs:
            run_record = RunRecord(
                id=run.run_id, experiment_id=experiment.id, task_id=run.task_id,
                candidate_id=run.candidate_id, seed=run.seed,
                status="completed", success=int(run.success), trajectory_score=run.trajectory_score,
                cost_cny=run.cost_cny, result=run.model_dump(mode="json", exclude={"events"}),
            )
            session.add(run_record)
            session.add_all([
                EventRecord(
                    run_id=run.run_id, seq=event.seq, type=event.type.value,
                    payload=event.payload, timestamp=event.timestamp,
                )
                for event in run.events
            ])


def persisted_counts() -> dict[str, int]:
    init_database()
    with session_factory()() as session:
        return {
            "experiments": session.query(ExperimentRecord).count(),
            "runs": session.query(RunRecord).count(),
            "events": session.query(EventRecord).count(),
        }


def update_experiment_status(experiment_id: str, status: str) -> None:
    """Keep cancellation/failure state auditable in the durable store."""
    init_database()
    with session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment_id)
        if record is not None:
            record.status = status
