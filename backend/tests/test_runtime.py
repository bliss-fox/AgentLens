import importlib.util
import json
from pathlib import Path

import httpx
import pytest

import agentlens.demo as demo_module
import agentlens.runtime as runtime_module
from agentlens.agent_client import AgentProtocolError
from agentlens.database import session_factory, tool_grant_token_hash
from agentlens.demo import CANDIDATES, TASKS, build_demo_experiment, run_scripted_trial
from agentlens.exceptions import ExperimentCancelled
from agentlens.models import ToolGrantRecord
from agentlens.runtime import resolve_candidate, run_http_trial


def sse_response(run_id: str, events: list[tuple[str, dict]], event_name: str = "trace"):
    chunks = []
    for seq, (event_type, payload) in enumerate(events):
        body = {"run_id": run_id, "seq": seq, "type": event_type, "payload": payload}
        chunks.append(f"event: {event_name}\ndata: {json.dumps(body)}\n\n")
    return "".join(chunks)


def test_http_trial_converts_valid_trace_to_run_result():
    captured = {}

    async def handler(request: httpx.Request):
        payload = json.loads(request.content)
        captured.update(payload)
        assert len(payload["cassette_content_sha256"]) == 64
        assert len(payload["tool_grant_token"]) >= 32
        body = sse_response(
            payload["run_id"],
            [
                ("run.started", {"seed": payload["seed"]}),
                ("tool.call", {"name": "search_customer", "arguments": {"customer_id": "C-1042"}}),
                ("tool.result", {"name": "search_customer", "result": {"id": "C-1042"}}),
                ("state", {"state": {"customer": {"id": "C-1042"}}}),
                ("usage", {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 0}),
                ("final", {"answer": "已找到客户 C-1042。", "verified": True}),
                ("run.completed", {"status": "success"}),
            ],
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    candidate = CANDIDATES["support-v1.4"].model_copy(
        update={
            "endpoint": "http://agent.test",
            "model_parameters": {
                "pricing_cny": {
                    "version": "test",
                    "input_per_million": 2,
                    "output_per_million": 8,
                    "cached_per_million": 0.5,
                    "tool_cost": 0,
                }
            },
        }
    )
    result = run_http_trial(
        candidate,
        TASKS[0],
        1,
        "http-success",
        transport=httpx.MockTransport(handler),
    )
    assert result.success is True
    assert result.final_state == {"customer": {"id": "C-1042"}}
    assert [call.name for call in result.trajectory] == ["search_customer"]
    assert result.cost_complete is True
    assert result.failures == []
    with session_factory()() as session:
        grant = session.get(
            ToolGrantRecord,
            tool_grant_token_hash(captured["tool_grant_token"]),
        )
        assert grant is not None
        assert grant.run_id == captured["run_id"]
        assert grant.status == "revoked"


def test_http_trial_turns_protocol_error_into_auditable_failed_run():
    async def handler(request: httpx.Request):
        payload = json.loads(request.content)
        body = sse_response(
            payload["run_id"],
            [
                ("run.started", {}),
                ("final", {"answer": "invalid"}),
                ("run.completed", {"status": "success"}),
            ],
            event_name="message",
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    candidate = CANDIDATES["support-v1.4"].model_copy(update={"endpoint": "http://agent.test"})
    result = run_http_trial(
        candidate,
        TASKS[0],
        1,
        "http-protocol-error",
        transport=httpx.MockTransport(handler),
    )
    assert result.success is False
    assert [event.type.value for event in result.events] == [
        "run.started",
        "error",
        "run.completed",
    ]
    assert result.failures[0].category == "protocol_error"
    assert "traceback" not in result.failures[0].explanation.lower()


def test_http_trial_maps_remote_http_failure_to_environment_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="private upstream"))
    candidate = CANDIDATES["support-v1.4"].model_copy(update={"endpoint": "http://agent.test"})
    result = run_http_trial(
        candidate,
        TASKS[0],
        1,
        "http-503",
        transport=transport,
    )
    assert result.success is False
    assert result.failures[0].category == "environment_error"
    assert "private upstream" not in result.failures[0].explanation



def test_http_trial_revokes_tool_grant_on_cancellation(monkeypatch):
    captured = {}

    async def cancelled_agent(endpoint, request, **kwargs):
        captured["token"] = request.tool_grant_token
        raise AgentProtocolError("cancelled", "cancelled")

    monkeypatch.setattr(runtime_module, "run_http_agent", cancelled_agent)
    candidate = CANDIDATES["support-v1.4"].model_copy(
        update={"endpoint": "http://agent.test"}
    )

    with pytest.raises(ExperimentCancelled):
        run_http_trial(candidate, TASKS[0], 1, "http-cancelled")

    with session_factory()() as session:
        grant = session.get(
            ToolGrantRecord,
            tool_grant_token_hash(captured["token"]),
        )
        assert grant is not None
        assert grant.status == "revoked"

def test_http_execution_mode_routes_every_trial_through_adapter(monkeypatch):
    calls = []

    def resolve(candidate):
        return candidate.model_copy(update={"endpoint": f"http://{candidate.id}.test"})

    def fake_http(candidate, task, seed, namespace, **kwargs):
        calls.append((candidate.id, task.id, seed))
        return run_scripted_trial(candidate.id, task, seed, namespace)

    monkeypatch.setattr(demo_module, "resolve_candidate", resolve)
    monkeypatch.setattr(demo_module, "run_http_trial", fake_http)
    experiment = build_demo_experiment(
        "http-routing",
        "test",
        repetitions=1,
        execution_mode="http",
    )
    assert len(calls) == 12
    assert experiment.candidate.endpoint == "http://support-v1.4.test"
    assert experiment.baseline.endpoint == "http://support-v1.3.test"


def test_candidate_endpoint_rejects_embedded_credentials():
    candidate = CANDIDATES["support-v1.4"].model_copy(
        update={"endpoint": "http://token:secret@agent.test"}
    )
    with pytest.raises(ValueError, match="credentials"):
        resolve_candidate(candidate)


def test_example_agent_satisfies_real_http_contract():
    path = Path(__file__).parents[2] / "examples" / "scripted_agent.py"
    spec = importlib.util.spec_from_file_location("scripted_example_agent", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    candidate = CANDIDATES["support-v1.4"].model_copy(
        update={"endpoint": "http://example-agent.test"}
    )
    result = run_http_trial(
        candidate,
        TASKS[0],
        1,
        "example-e2e",
        transport=httpx.ASGITransport(app=module.app),
    )
    assert result.success is True
    assert result.final_answer == "已找到客户 C-1042。"
