import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from candidate_agent.app import (
    AgentRunRequest,
    ModelToolCall,
    ModelTurn,
    OpenAIModelAdapter,
    Settings,
    create_app,
    stream_run,
)


class ToolThenFinalModel:
    async def complete(self, messages, seed):
        if any(message.get("role") == "tool" for message in messages):
            return ModelTurn(
                text="工具验证完成。",
                input_tokens=30,
                output_tokens=8,
            )
        return ModelTurn(
            tool_calls=[
                ModelToolCall(
                    id="call-1",
                    name="analyze_code",
                    arguments={"code": "try:\n    pass\nexcept:\n    pass"},
                )
            ],
            input_tokens=20,
            output_tokens=10,
        )


def run_request():
    return AgentRunRequest(
        run_id="run-candidate-1",
        task_id="static-analysis",
        task_input={"message": "请做静态分析"},
        seed=3,
        environment_snapshot="coding-agent-v1:runner-2026-09-14",
        cassette_content_sha256="a" * 64,
        tool_grant_token="x" * 40,
        tool_gateway_url="http://gateway.test/api/v1/tools/invoke",
    )


def decode_events(chunks):
    events = []
    for chunk in chunks:
        text = chunk.decode("utf-8")
        assert text.startswith("event: trace\ndata: ")
        events.append(json.loads(text.split("data: ", 1)[1]))
    return events


@pytest.mark.asyncio
async def test_adapter_emits_strict_trace_and_routes_tools_through_gateway():
    gateway_requests = []

    async def gateway(request: httpx.Request):
        gateway_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "result": {
                    "ok": True,
                    "finding_codes": ["BARE_EXCEPT"],
                },
                "source": "constrained_runner",
            },
        )

    chunks = [
        chunk
        async for chunk in stream_run(
            run_request(),
            ToolThenFinalModel(),
            transport=httpx.MockTransport(gateway),
        )
    ]
    events = decode_events(chunks)

    assert [event["seq"] for event in events] == list(range(len(events)))
    assert [event["type"] for event in events] == [
        "run.started",
        "message",
        "usage",
        "tool.call",
        "tool.result",
        "state",
        "usage",
        "final",
        "run.completed",
    ]
    assert events[-1]["payload"]["status"] == "success"
    assert events[-2]["payload"]["final_state"]["tool_results"]["analyze_code"][
        "finding_codes"
    ] == ["BARE_EXCEPT"]
    assert gateway_requests[0]["tool_grant_token"] == "x" * 40
    assert gateway_requests[0]["cassette_id"] == "coding-agent-v1"


def test_fake_model_keeps_readiness_independent_of_api_key():
    app = create_app(model=ToolThenFinalModel())
    route_paths = {route.path for route in app.routes}
    assert {"/health", "/ready", "/v1/agent-runs"} <= route_paths

    readiness = TestClient(app).get("/ready").json()
    assert readiness["provider"] == "fake"
    assert readiness["identity"]["schema"] == "agentlens.candidate-identity/v1"
    assert readiness["identity"]["prompt_hash"].startswith("sha256:")
    assert readiness["identity"]["tool_schema_hash"].startswith("sha256:")


def test_deepseek_uses_openai_compatible_provider_settings():
    settings = Settings(
        llm_provider="deepseek",
        deepseek_api_key="test-key",
        llm_model="deepseek-chat",
    )

    assert settings.ready is True
    assert settings.api_key == "test-key"
    assert settings.model_base_url == "https://api.deepseek.com/v1"
    assert settings.resolved_model == "deepseek-chat"
    assert settings.missing_key_name == "DEEPSEEK_API_KEY"
    assert settings.pricing_cny == {
        "version": "adapter-config@2026-09-14",
        "input_per_million": 2.0,
        "output_per_million": 8.0,
        "cached_per_million": 0.5,
        "tool_cost": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "expects_seed"),
    [("openai", True), ("deepseek", False)],
)
async def test_real_adapter_only_sends_seed_to_supported_provider(
    provider, expects_seed
):
    settings = Settings(
        llm_provider=provider,
        openai_api_key="test-key",
        deepseek_api_key="test-key",
    )
    adapter = OpenAIModelAdapter(settings)
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="done", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=None,
        )

    adapter._client.chat.completions.create = create
    await adapter.complete([{"role": "user", "content": "hello"}], seed=17)

    assert ("seed" in captured) is expects_seed
    if expects_seed:
        assert captured["seed"] == 17
