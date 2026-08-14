from fastapi.testclient import TestClient

from agentlens.api import app

client = TestClient(app)


def test_bootstrap_and_experiment_creation():
    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    assert len(bootstrap.json()["experiment"]["runs"]) == 60
    created = client.post("/api/v1/experiments", json={
        "prompt": "重新测评", "candidate_id": "support-v1.4", "baseline_candidate_id": "support-v1.3",
        "benchmark_id": "customer-tools-v2", "repetitions": 10,
    })
    assert created.status_code == 200
    assert created.json()["total_runs"] == 60


def test_tool_gateway_replays_and_fails_closed():
    hit = client.post("/api/v1/tools/invoke", json={
        "cassette_id": "customer-tools-v2", "mode": "replay", "tool": "search_customer",
        "arguments": {"customer_id": "C-1042"},
    })
    assert hit.status_code == 200
    assert hit.json()["source"] == "frozen_cassette"
    miss = client.post("/api/v1/tools/invoke", json={
        "cassette_id": "customer-tools-v2", "mode": "replay", "tool": "search_customer",
        "arguments": {"customer_id": "missing"},
    })
    assert miss.status_code == 424


def test_health_exposes_persisted_audit_counts():
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["storage"]["experiments"] >= 1
    assert health.json()["storage"]["runs"] >= 60


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
    assert event_names == ["experiment.progress"] * 6 + ["experiment.result"]


def test_cancelled_experiment_stream_has_typed_terminal_event():
    created = client.post("/api/v1/experiments", json={"prompt": "Cancel integration"}).json()
    cancelled = client.post(f"/api/v1/experiments/{created['id']}/cancel")
    assert cancelled.status_code == 200
    response = client.get(f"/api/v1/experiments/{created['id']}/events")
    assert "event: experiment.cancelled" in response.text
    assert "event: experiment.result" not in response.text
