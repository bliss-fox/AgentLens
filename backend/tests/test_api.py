import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import agentlens.api as api_module
import agentlens.application_dependencies as dependency_module
import agentlens.demo as demo_module
import agentlens.routers.experiments as experiments_router
import agentlens.routers.health as health_router
import agentlens.routers.reviews as reviews_router
from agentlens.api import app, create_app
from agentlens.database import (
    claim_experiment,
    claim_failure_review,
    engine,
    issue_tool_grant,
    release_failure_review_claim,
    revoke_tool_grant,
    session_factory,
    tool_grant_token_hash,
)
from agentlens.judge import JudgeReviewError, JudgeVerdict
from agentlens.models import (
    Base,
    EventRecord,
    ExperimentRecord,
    RunRecord,
    ToolGrantRecord,
)
from agentlens.schemas import ExperimentRequest
from agentlens.store import ExperimentStore
from agentlens.tool_gateway import CassetteRegistry

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_api_database():
    if str(engine().url) != "sqlite+pysqlite:///:memory:":
        raise RuntimeError("tests may only reset the isolated in-memory database")
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    cassette_registry = CassetteRegistry()
    app.state.cassette_registry = cassette_registry
    app.state.experiment_store = ExperimentStore(
        seed_demo=True,
        cassette_registry=cassette_registry,
    )
    app.state.redis_pool = None


def test_create_app_defers_default_dependencies(monkeypatch):
    constructor_calls = []

    class UnexpectedStore:
        def __init__(self, *args, **kwargs):
            constructor_calls.append(("store", args, kwargs))

    class UnexpectedRegistry:
        def __init__(self, *args, **kwargs):
            constructor_calls.append(("registry", args, kwargs))

    monkeypatch.setattr(dependency_module, "ExperimentStore", UnexpectedStore)
    monkeypatch.setattr(dependency_module, "CassetteRegistry", UnexpectedRegistry)

    factory_app = create_app()

    assert factory_app.state.experiment_store is None
    assert factory_app.state.cassette_registry is None
    assert constructor_calls == []


def test_create_app_preserves_explicit_dependencies():
    injected_store = object()
    injected_registry = object()

    factory_app = create_app(
        experiment_store=injected_store,
        cassette_registry=injected_registry,
        seed_demo=False,
    )

    assert factory_app.state.experiment_store is injected_store
    assert factory_app.state.cassette_registry is injected_registry
    assert factory_app.state.seed_demo is False
    response = TestClient(factory_app).get("/api/v1/tasks")
    assert response.status_code == 200
    assert response.json()


@pytest.mark.asyncio
async def test_lifespan_shuts_down_local_background_workers(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: type("Settings", (), {"execution_backend": "local"})(),
    )
    monkeypatch.setattr(app.state.experiment_store, "shutdown", lambda: calls.append("shutdown"))

    async with api_module.lifespan(app):
        assert app.state.redis_pool is None

    assert calls == ["shutdown"]


def test_bootstrap_and_experiment_creation():
    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["experiment"]["status"] == "completed"
    assert len(bootstrap.json()["experiment"]["runs"]) >= 1
    created = client.post(
        "/api/v1/experiments",
        json={
            "prompt": "重新测评",
            "candidate_id": "support-v1.4",
            "baseline_candidate_id": "support-v1.3",
            "benchmark_id": "customer-tools-v2",
            "repetitions": 10,
        },
    )
    assert created.status_code == 202
    assert created.json()["status"] == "queued"
    assert created.json()["completed_runs"] == 0
    assert created.json()["total_runs"] == 120
    deadline = time.monotonic() + 2
    current = created.json()
    while current["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
        current = client.get(f"/api/v1/experiments/{current['id']}").json()
    assert current["status"] == "completed"
    assert current["completed_runs"] == current["total_runs"] == 120


def test_tool_gateway_replays_and_fails_closed():
    snapshot = client.get("/api/v1/cassettes/customer-tools-v2")
    assert snapshot.status_code == 200
    payload = snapshot.json()
    cassette_sha256 = payload["content_sha256"]
    assert payload["mode"] == "replay"
    assert payload["entries"] == 3
    assert len(cassette_sha256) == 64
    assert "林晓" not in snapshot.text
    assert "paid" not in snapshot.text

    run_id = "api-tool-run"
    token = issue_tool_grant(
        run_id=run_id,
        experiment_id="api-tool-experiment",
        cassette_id="customer-tools-v2",
        cassette_content_sha256=cassette_sha256,
        allowed_tools=["search_customer"],
        max_calls=2,
        ttl_seconds=60,
    )
    hit_payload = {
        "run_id": run_id,
        "tool_grant_token": token,
        "cassette_id": "customer-tools-v2",
        "cassette_content_sha256": cassette_sha256,
        "mode": "replay",
        "tool": "search_customer",
        "arguments": {"customer_id": "C-1042"},
    }
    hit = client.post("/api/v1/tools/invoke", json=hit_payload)
    assert hit.status_code == 200
    assert hit.json()["source"] == "frozen_cassette"

    miss_payload = {
        **hit_payload,
        "arguments": {"customer_id": "missing"},
    }
    miss = client.post("/api/v1/tools/invoke", json=miss_payload)
    assert miss.status_code == 424
    assert miss.json()["detail"]["category"] == "environment_error"
    assert "traceback" not in miss.text.lower()

    drifted = client.post(
        "/api/v1/tools/invoke",
        json={**hit_payload, "cassette_content_sha256": "0" * 64},
    )
    assert drifted.status_code == 403
    assert drifted.json()["detail"] == {
        "category": "tool_authorization_error",
        "message": "tool grant does not authorize this invocation",
        "retryable": False,
    }

    malformed_digest = client.post(
        "/api/v1/tools/invoke",
        json={**hit_payload, "cassette_content_sha256": "not-a-sha256"},
    )
    assert malformed_digest.status_code == 422
    assert "not-a-sha256" not in malformed_digest.text

    sensitive_token = "sensitive token with spaces 1234567890"
    malformed_token = client.post(
        "/api/v1/tools/invoke",
        json={**hit_payload, "tool_grant_token": sensitive_token},
    )
    assert malformed_token.status_code == 422
    assert sensitive_token not in malformed_token.text
    assert {"type", "loc", "msg"} == set(malformed_token.json()["detail"][0])

    missing_digest_payload = dict(hit_payload)
    missing_digest_payload.pop("cassette_content_sha256")
    missing_digest = client.post("/api/v1/tools/invoke", json=missing_digest_payload)
    assert missing_digest.status_code == 422

    record_mode = client.post(
        "/api/v1/tools/invoke",
        json={**hit_payload, "mode": "record"},
    )
    assert record_mode.status_code == 422

    unknown_run_id = "api-unknown-cassette"
    unknown_token = issue_tool_grant(
        run_id=unknown_run_id,
        experiment_id="api-tool-experiment",
        cassette_id="missing",
        cassette_content_sha256=cassette_sha256,
        allowed_tools=["search_customer"],
        max_calls=1,
        ttl_seconds=60,
    )
    unknown_cassette = client.post(
        "/api/v1/tools/invoke",
        json={
            **hit_payload,
            "run_id": unknown_run_id,
            "tool_grant_token": unknown_token,
            "cassette_id": "missing",
        },
    )
    assert unknown_cassette.status_code == 424
    assert revoke_tool_grant(token)
    assert revoke_tool_grant(unknown_token)


def test_tool_gateway_rejects_cross_run_reuse_and_enforces_budget():
    snapshot = client.get("/api/v1/cassettes/customer-tools-v2").json()
    run_id = "api-budget-run"
    token = issue_tool_grant(
        run_id=run_id,
        experiment_id="api-budget-experiment",
        cassette_id="customer-tools-v2",
        cassette_content_sha256=snapshot["content_sha256"],
        allowed_tools=["search_customer"],
        max_calls=1,
        ttl_seconds=60,
    )
    payload = {
        "run_id": run_id,
        "tool_grant_token": token,
        "cassette_id": "customer-tools-v2",
        "cassette_content_sha256": snapshot["content_sha256"],
        "mode": "replay",
        "tool": "search_customer",
        "arguments": {"customer_id": "C-1042"},
    }

    invalid_token = client.post(
        "/api/v1/tools/invoke",
        json={**payload, "tool_grant_token": "x" * 43},
    )
    assert invalid_token.status_code == 403
    assert token not in invalid_token.text

    cross_run = client.post(
        "/api/v1/tools/invoke",
        json={**payload, "run_id": "another-run"},
    )
    assert cross_run.status_code == 403

    disallowed_tool = client.post(
        "/api/v1/tools/invoke",
        json={**payload, "tool": "get_order", "arguments": {"order_id": "O-8891"}},
    )
    assert disallowed_tool.status_code == 403

    allowed = client.post("/api/v1/tools/invoke", json=payload)
    assert allowed.status_code == 200
    exhausted = client.post("/api/v1/tools/invoke", json=payload)
    assert exhausted.status_code == 429
    assert exhausted.json()["detail"]["category"] == "tool_budget_exhausted"

    assert revoke_tool_grant(token)
    revoked = client.post("/api/v1/tools/invoke", json=payload)
    assert revoked.status_code == 403

def test_invalid_experiment_references_return_422():
    invalid_candidate = client.post(
        "/api/v1/experiments",
        json={
            "prompt": "invalid",
            "candidate_id": "missing",
        },
    )
    assert invalid_candidate.status_code == 422
    assert invalid_candidate.json()["detail"]["category"] == "invalid_candidate"

    invalid_benchmark = client.post(
        "/api/v1/experiments",
        json={
            "prompt": "invalid",
            "benchmark_id": "missing",
        },
    )
    assert invalid_benchmark.status_code == 422
    assert invalid_benchmark.json()["detail"]["category"] == "invalid_benchmark"

    empty_prompt = client.post("/api/v1/experiments", json={"prompt": ""})
    assert empty_prompt.status_code == 422


def test_http_execution_requires_configured_candidate_endpoints(monkeypatch):
    def missing(candidate):
        raise ValueError(f"missing endpoint for {candidate.id}")

    monkeypatch.setattr(experiments_router, "resolve_candidate", missing)
    response = client.post(
        "/api/v1/experiments",
        json={"prompt": "missing endpoints", "execution_mode": "http"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["category"] == "invalid_execution_config"


def test_http_execution_can_be_queued_with_configured_endpoints(monkeypatch):
    started = []

    def configured(candidate):
        return candidate.model_copy(update={"endpoint": f"http://{candidate.id}.test"})

    monkeypatch.setattr(experiments_router, "resolve_candidate", configured)
    monkeypatch.setattr(demo_module, "resolve_candidate", configured)
    monkeypatch.setattr(
        app.state.experiment_store,
        "start_local",
        lambda experiment_id: started.append(experiment_id),
    )
    response = client.post(
        "/api/v1/experiments",
        json={"prompt": "HTTP mode", "execution_mode": "http", "repetitions": 1},
    )
    assert response.status_code == 202
    assert response.json()["candidate"]["endpoint"] == "http://support-v1.4.test"
    assert started == [response.json()["id"]]
    assert app.state.experiment_store.cancel(response.json()["id"]) is not None


def test_real_candidate_must_complete_readiness_identity_handshake():
    response = client.post(
        "/api/v1/experiments",
        json={
            "prompt": "real candidate",
            "candidate_id": "coding-assistant-v1",
            "baseline_candidate_id": None,
            "benchmark_id": "coding-agent-core-v1",
            "execution_mode": "http",
            "repetitions": 1,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["category"] == "invalid_execution_config"
    assert "connection test" in response.json()["detail"]["message"]


def test_health_exposes_persisted_audit_counts():
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["database_status"] == "ok"
    assert health.json()["schema_revision"] is None
    assert health.json()["queue_status"] == "not_required"
    assert health.json()["worker_status"] == "not_required"
    assert health.json()["storage"]["experiments"] >= 1
    assert health.json()["storage"]["runs"] >= 120
    grant_health = health.json()["storage"]["tool_grants"]
    assert grant_health == {
        "active": 0,
        "expired": 0,
        "revoked": 0,
        "other": 0,
        "total": 0,
        "expired_marked": 0,
        "purged": 0,
        "retention_seconds": 604800,
        "cleanup_batch_size": 1000,
    }


def test_health_purges_only_tool_grants_past_retention():
    settings = health_router.get_settings()
    current_time = datetime.now(UTC)
    token = issue_tool_grant(
        run_id="run-health-retention",
        experiment_id="exp-health-retention",
        cassette_id="customer-tools-v2",
        cassette_content_sha256="a" * 64,
        allowed_tools=["search_customer"],
        max_calls=1,
        ttl_seconds=300,
        now=current_time
        - timedelta(seconds=settings.tool_grant_retention_seconds + 600),
    )
    assert revoke_tool_grant(
        token,
        now=current_time
        - timedelta(seconds=settings.tool_grant_retention_seconds),
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["storage"]["tool_grants"]["purged"] == 1
    with session_factory()() as session:
        assert session.get(ToolGrantRecord, tool_grant_token_hash(token)) is None


def test_health_degrades_without_leaking_database_error(monkeypatch):
    def unavailable_database():
        raise RuntimeError("postgresql://user:secret@database")

    monkeypatch.setattr(health_router, "schema_revision", unavailable_database)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database_status"] == "unavailable"
    assert response.json()["schema_revision"] is None
    assert response.json()["storage"] is None
    assert response.json()["queue_status"] == "not_required"
    assert response.json()["worker_status"] == "not_required"
    assert "secret" not in response.text


def test_health_reaps_running_experiment_with_expired_heartbeat():
    queued = app.state.experiment_store.create_queued(
        ExperimentRequest(prompt="stale health recovery", repetitions=1)
    )
    assert claim_experiment(queued.id, now=datetime(2020, 1, 1, tzinfo=UTC))

    response = client.get("/health")

    assert response.status_code == 200
    recovered = app.state.experiment_store.get(queued.id)
    assert recovered is not None
    assert recovered.status == "failed"
    events = client.get(f"/api/v1/experiments/{queued.id}/events")
    assert "event: experiment.error" in events.text
    assert '"category": "worker_lost"' in events.text


def test_sse_reaps_expired_running_experiment_without_health_poll():
    queued = app.state.experiment_store.create_queued(
        ExperimentRequest(prompt="stale SSE recovery", repetitions=1)
    )
    assert claim_experiment(queued.id, now=datetime(2020, 1, 1, tzinfo=UTC))

    response = client.get(f"/api/v1/experiments/{queued.id}/events")

    assert response.status_code == 200
    assert response.text.count("event: experiment.error") == 1
    assert '"category": "worker_lost"' in response.text
    assert '"status": "failed"' in response.text
    assert '"experiment": {' in response.text
    assert app.state.experiment_store.get(queued.id).status == "failed"


def test_arq_health_degrades_when_redis_ping_fails(monkeypatch):
    class UnavailablePool:
        async def ping(self):
            raise RuntimeError("redis://user:secret@queue")

    monkeypatch.setattr(
        health_router,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "execution_backend": "arq",
                "tool_grant_retention_seconds": 604800,
                "tool_grant_cleanup_batch_size": 1000,
            },
        )(),
    )
    monkeypatch.setattr(
        health_router,
        "schema_revision",
        lambda: health_router.EXPECTED_SCHEMA_REVISION,
    )
    monkeypatch.setattr(app.state, "redis_pool", UnavailablePool(), raising=False)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["queue_status"] == "unavailable"
    assert response.json()["worker_status"] == "unavailable"
    assert "secret" not in response.text


@pytest.mark.parametrize(
    ("heartbeat", "expected_status", "worker_status"),
    [
        (None, 503, "unavailable"),
        (b"Aug-23 12:00:00 j_complete=1", 200, "ok"),
    ],
)
def test_arq_health_requires_fresh_worker_heartbeat(
    monkeypatch,
    heartbeat,
    expected_status,
    worker_status,
):
    class HeartbeatPool:
        async def ping(self):
            return True

        async def get(self, key):
            assert key == health_router.ARQ_WORKER_HEALTH_CHECK_KEY
            return heartbeat

    monkeypatch.setattr(
        health_router,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "execution_backend": "arq",
                "tool_grant_retention_seconds": 604800,
                "tool_grant_cleanup_batch_size": 1000,
            },
        )(),
    )
    monkeypatch.setattr(
        health_router,
        "schema_revision",
        lambda: health_router.EXPECTED_SCHEMA_REVISION,
    )
    monkeypatch.setattr(app.state, "redis_pool", HeartbeatPool(), raising=False)

    response = client.get("/health")

    assert response.status_code == expected_status
    assert response.json()["queue_status"] == "ok"
    assert response.json()["worker_status"] == worker_status
    assert "j_complete" not in response.text


def test_experiment_sse_has_ordered_progress_and_result_terminal_event():
    created = client.post("/api/v1/experiments", json={"prompt": "SSE integration"}).json()
    response = client.get(f"/api/v1/experiments/{created['id']}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    event_names = [
        line.removeprefix("event: ")
        for line in response.text.splitlines()
        if line.startswith("event: ")
    ]
    assert event_names[-1] == "experiment.result"
    assert event_names.count("experiment.result") == 1
    assert "experiment.error" not in event_names
    assert all(name == "experiment.progress" for name in event_names[:-1])


def test_cancelled_experiment_stream_has_typed_terminal_event():
    created = client.post("/api/v1/experiments", json={"prompt": "Cancel integration"}).json()
    cancelled = client.post(f"/api/v1/experiments/{created['id']}/cancel")
    assert cancelled.status_code == 200
    response = client.get(f"/api/v1/experiments/{created['id']}/events")
    assert "event: experiment.cancelled" in response.text
    assert "event: experiment.result" not in response.text


def test_arq_backend_enqueues_persisted_experiment(monkeypatch):
    calls = []

    class FakePool:
        async def enqueue_job(self, *args, **kwargs):
            calls.append((args, kwargs))
            return object()

    monkeypatch.setattr(
        experiments_router,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "execution_backend": "arq",
                "tool_grant_retention_seconds": 604800,
                "tool_grant_cleanup_batch_size": 1000,
            },
        )(),
    )
    app.state.redis_pool = FakePool()
    response = client.post(
        "/api/v1/experiments",
        json={"prompt": "ARQ enqueue contract", "repetitions": 1},
    )
    assert response.status_code == 202
    experiment = response.json()
    assert experiment["status"] == "queued"
    assert calls[0][0] == ("execute_experiment", experiment["id"])
    assert calls[0][1]["_job_id"] == experiment["id"]
    assert app.state.experiment_store.get(experiment["id"]).status == "queued"


@pytest.mark.parametrize(
    ("outcome", "category", "message"),
    [
        ("exception", "queue_unavailable", "experiment queue unavailable"),
        ("rejected", "queue_rejected", "experiment queue rejected"),
    ],
)
def test_arq_queue_failures_are_persisted_and_structured(
    monkeypatch,
    outcome,
    category,
    message,
):
    class FailedPool:
        async def enqueue_job(self, *args, **kwargs):
            if outcome == "exception":
                raise RuntimeError("redis://user:secret@queue")

    monkeypatch.setattr(
        experiments_router,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "execution_backend": "arq",
                "tool_grant_retention_seconds": 604800,
                "tool_grant_cleanup_batch_size": 1000,
            },
        )(),
    )
    monkeypatch.setattr(app.state, "redis_pool", FailedPool(), raising=False)

    response = client.post(
        "/api/v1/experiments",
        json={"prompt": "queue failure contract", "repetitions": 1},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["category"] == category
    assert detail["message"] == message
    assert detail["retryable"] is True
    assert "secret" not in response.text
    experiment = app.state.experiment_store.get(detail["experiment_id"])
    assert experiment is not None
    assert experiment.status == "failed"
    events = client.get(f"/api/v1/experiments/{experiment.id}/events")
    assert f'"category": "{category}"' in events.text
    assert '"experiment": {' in events.text
    assert '"status": "failed"' in events.text
    assert "event: experiment.error" in events.text


def test_local_executor_start_failure_is_persisted_and_structured(monkeypatch):
    def fail_start(experiment_id):
        raise RuntimeError("thread scheduler secret")

    monkeypatch.setattr(
        experiments_router,
        "get_settings",
        lambda: type("Settings", (), {"execution_backend": "local"})(),
    )
    monkeypatch.setattr(app.state.experiment_store, "start_local", fail_start)

    response = client.post(
        "/api/v1/experiments",
        json={"prompt": "local executor failure contract", "repetitions": 1},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == {
        "category": "executor_unavailable",
        "message": "local experiment executor unavailable",
        "experiment_id": detail["experiment_id"],
        "retryable": True,
    }
    assert "secret" not in response.text
    experiment = app.state.experiment_store.get(detail["experiment_id"])
    assert experiment is not None
    assert experiment.status == "failed"
    events = client.get(f"/api/v1/experiments/{experiment.id}/events")
    assert '"category": "executor_unavailable"' in events.text
    assert '"status": "failed"' in events.text
    assert "event: experiment.error" in events.text


def first_failure_target():
    experiment = app.state.experiment_store.latest()
    run = next(
        run
        for run in (*experiment.runs, *experiment.baseline_runs)
        if run.failures
    )
    return experiment, run, run.failures[0]


def test_on_demand_judge_stays_offline_without_api_key(monkeypatch):
    experiment, run, failure = first_failure_target()

    async def offline_review(payload):
        return None

    monkeypatch.setattr(reviews_router, "semantic_failure_review", offline_review)
    response = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "category": "judge_unavailable",
        "message": "semantic judge is not configured",
        "retryable": False,
    }
    unchanged = app.state.experiment_store.get(experiment.id)
    unchanged_run = next(item for item in (*unchanged.runs, *unchanged.baseline_runs) if item.run_id == run.run_id)
    assert unchanged_run.failures[0] == failure


def test_on_demand_judge_persists_valid_review_atomically(monkeypatch):
    experiment, run, failure = first_failure_target()
    captured = {}
    model_calls = 0

    async def fake_review(payload):
        nonlocal model_calls
        model_calls += 1
        captured.update(payload)
        return JudgeVerdict(
            supported=True,
            confidence="medium",
            category=failure.category,
            evidence_sequences=[failure.event_range[0]],
            explanation="reviewed from persisted trace",
        )

    monkeypatch.setattr(reviews_router, "semantic_failure_review", fake_review)
    response = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["failure"]["judge_verdict"] == "support"
    assert body["failure"]["confidence"] == "medium"
    assert body["failure"]["judge_explanation"] == "reviewed from persisted trace"
    assert body["failure"]["judge_evidence_sequences"] == [failure.event_range[0]]
    assert captured["experiment_id"] == experiment.id
    assert captured["run_id"] == run.run_id
    assert captured["failure"]["rule"] == failure.rule
    assert captured["events"]
    assert all(
        failure.event_range[0] <= event["seq"] <= failure.event_range[1]
        for event in captured["events"]
    )
    assert {event["seq"] for event in captured["events"]} == {
        event.seq
        for event in run.events
        if failure.event_range[0] <= event.seq <= failure.event_range[1]
    }

    reloaded = app.state.experiment_store.get(experiment.id)
    reloaded_run = next(
        item
        for item in (*reloaded.runs, *reloaded.baseline_runs)
        if item.run_id == run.run_id
    )
    assert reloaded_run.failures[0].judge_verdict == "support"
    with session_factory()() as session:
        run_record = session.get(RunRecord, run.run_id)
        assert run_record is not None
        assert run_record.result["failures"][0]["judge_verdict"] == "support"

    duplicate = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["category"] == "review_already_exists"
    assert model_calls == 1

    forced = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review?force=true"
    )
    assert forced.status_code == 200
    assert model_calls == 2


def test_on_demand_judge_releases_claim_after_external_failure(monkeypatch):
    experiment, run, failure = first_failure_target()
    model_calls = 0

    async def flaky_review(payload):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            raise JudgeReviewError(
                "judge_timeout",
                "semantic judge request timed out",
                retryable=True,
            )
        return JudgeVerdict(
            supported=True,
            confidence="medium",
            category=failure.category,
            evidence_sequences=[failure.event_range[0]],
            explanation="retry succeeded after released claim",
        )

    monkeypatch.setattr(reviews_router, "semantic_failure_review", flaky_review)
    path = (
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )

    failed = client.post(path)
    retried = client.post(path)

    assert failed.status_code == 504
    assert retried.status_code == 200
    assert model_calls == 2


def test_on_demand_judge_rejects_active_claim_before_model_call(monkeypatch):
    experiment, run, _ = first_failure_target()
    called = False
    claim = claim_failure_review(
        experiment.id,
        run.run_id,
        0,
        force=False,
        lease_seconds=60,
    )

    async def unexpected_review(payload):
        nonlocal called
        called = True
        raise AssertionError("active claim called the Judge")

    monkeypatch.setattr(reviews_router, "semantic_failure_review", unexpected_review)
    response = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review?force=true"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "category": "review_in_progress",
        "message": "failure review is already in progress",
        "retryable": True,
    }
    assert called is False
    assert release_failure_review_claim(
        experiment.id,
        run.run_id,
        0,
        claim.token,
    )


def test_on_demand_judge_rejects_inconsistent_run_before_model_call(monkeypatch):
    experiment, run, _ = first_failure_target()
    called = False

    async def unexpected_review(payload):
        nonlocal called
        called = True
        raise AssertionError("inconsistent run called the Judge")

    with session_factory()() as session, session.begin():
        run_record = session.get(RunRecord, run.run_id)
        assert run_record is not None
        run_record.status = "failed"
    monkeypatch.setattr(reviews_router, "semantic_failure_review", unexpected_review)

    response = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "category": "failure_context_unavailable",
        "message": "persisted run context is unavailable",
        "retryable": False,
    }
    assert called is False


def test_on_demand_judge_rejects_inconsistent_event_audit_before_model_call(
    monkeypatch,
):
    experiment, run, failure = first_failure_target()
    called = False

    async def unexpected_review(payload):
        nonlocal called
        called = True
        raise AssertionError("inconsistent event audit called the Judge")

    with session_factory()() as session, session.begin():
        event_record = (
            session.query(EventRecord)
            .filter(
                EventRecord.run_id == run.run_id,
                EventRecord.seq == failure.event_range[0],
            )
            .one()
        )
        event_record.payload = {**event_record.payload, "tampered": True}
    monkeypatch.setattr(reviews_router, "semantic_failure_review", unexpected_review)

    response = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "category": "failure_context_unavailable",
        "message": "persisted event evidence is inconsistent",
        "retryable": False,
    }
    assert called is False


def test_on_demand_judge_rejects_non_completed_experiment_before_model_call(
    monkeypatch,
):
    experiment, run, _ = first_failure_target()
    called = False

    async def unexpected_review(payload):
        nonlocal called
        called = True
        raise AssertionError("non-completed experiment called the Judge")

    with session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, experiment.id)
        assert record is not None
        record.status = "running"
    monkeypatch.setattr(reviews_router, "semantic_failure_review", unexpected_review)

    response = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "category": "review_not_ready",
        "message": "only completed experiments can be reviewed",
        "retryable": True,
    }
    assert called is False


def test_on_demand_judge_rejects_invalid_citations_without_persistence(monkeypatch):
    experiment, run, failure = first_failure_target()

    async def invalid_review(payload):
        return JudgeVerdict(
            supported=True,
            confidence="high",
            category=failure.category,
            evidence_sequences=[failure.event_range[1] + 1],
            explanation="citation is outside the deterministic attribution",
        )

    monkeypatch.setattr(reviews_router, "semantic_failure_review", invalid_review)
    response = client.post(
        f"/api/v1/experiments/{experiment.id}/runs/{run.run_id}/"
        "failures/0/review"
    )

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "category": "judge_invalid_response",
        "message": "semantic judge returned invalid evidence citations",
        "retryable": False,
    }
    reloaded = app.state.experiment_store.get(experiment.id)
    reloaded_run = next(
        item
        for item in (*reloaded.runs, *reloaded.baseline_runs)
        if item.run_id == run.run_id
    )
    assert reloaded_run.failures[0] == failure
    with session_factory()() as session:
        run_record = session.get(RunRecord, run.run_id)
        assert run_record is not None
        assert run_record.result["failures"][0]["judge_verdict"] == "not_run"


def test_dynamic_candidate_metadata_is_used_only_for_http_mode(monkeypatch):
    left = demo_module.CANDIDATES["support-v1.4"].model_copy(
        update={
            "id": "remote-v2",
            "name": "Remote Agent",
            "version": "2026.08",
            "model": "remote-model-v2",
            "prompt_hash": "sha256:remote-v2",
            "scaffold_version": "remote@2",
            "endpoint": "http://remote-v2.test",
        }
    )
    right = demo_module.CANDIDATES["support-v1.3"].model_copy(
        update={
            "id": "remote-v1",
            "name": "Remote Agent",
            "version": "2026.07",
            "model": "remote-model-v1",
            "prompt_hash": "sha256:remote-v1",
            "scaffold_version": "remote@1",
            "endpoint": "http://remote-v1.test",
        }
    )
    catalog = {**demo_module.CANDIDATES, left.id: left, right.id: right}
    started = []
    monkeypatch.setattr(experiments_router, "candidate_catalog", lambda: catalog)
    monkeypatch.setattr(demo_module, "candidate_catalog", lambda: catalog)
    monkeypatch.setattr(
        app.state.experiment_store,
        "start_local",
        lambda experiment_id: started.append(experiment_id),
    )

    response = client.post(
        "/api/v1/experiments",
        json={
            "prompt": "dynamic candidates",
            "candidate_id": left.id,
            "baseline_candidate_id": right.id,
            "execution_mode": "http",
            "repetitions": 1,
        },
    )
    assert response.status_code == 202
    assert response.json()["candidate"]["model"] == "remote-model-v2"
    assert response.json()["baseline"]["prompt_hash"] == "sha256:remote-v1"
    assert started == [response.json()["id"]]
    assert app.state.experiment_store.cancel(response.json()["id"]) is not None

    scripted = client.post(
        "/api/v1/experiments",
        json={
            "prompt": "must stay fixture-only",
            "candidate_id": left.id,
            "baseline_candidate_id": right.id,
            "execution_mode": "scripted",
        },
    )
    assert scripted.status_code == 422
    assert scripted.json()["detail"]["category"] == "invalid_candidate"
