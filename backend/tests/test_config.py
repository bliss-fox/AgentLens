import json

import pytest
from pydantic import ValidationError

from agentlens.config import Settings
from agentlens.schemas import AgentRunRequest


def candidate_payload(candidate_id):
    return {
        "id": candidate_id,
        "name": "Remote support agent",
        "version": "2026.08",
        "model": "remote-model",
        "model_parameters": {"temperature": 0.1},
        "prompt_hash": "sha256:remote-prompt",
        "scaffold_version": "remote-scaffold@2",
        "tool_schema_hash": "sha256:remote-tools",
        "endpoint": "http://remote-agent.test",
    }


def test_candidate_overrides_parse_as_validated_specs(monkeypatch):
    monkeypatch.setenv(
        "CANDIDATE_OVERRIDES",
        json.dumps({"remote-v2": candidate_payload("remote-v2")}),
    )
    settings = Settings(_env_file=None)
    candidate = settings.candidate_overrides["remote-v2"]
    assert candidate.id == "remote-v2"
    assert candidate.model == "remote-model"
    assert candidate.endpoint == "http://remote-agent.test"


def test_candidate_override_key_must_match_nested_id():
    with pytest.raises(ValidationError, match="keys must match"):
        Settings(
            _env_file=None,
            candidate_overrides={"catalog-key": candidate_payload("different-id")},
        )


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("gateway/internal", "absolute HTTP"),
        ("https://user:secret@gateway.test/invoke", "credentials"),
        ("https://gateway.test/invoke?token=secret", "query"),
        ("https://gateway.test/invoke#token", "fragment"),
    ],
)
def test_tool_gateway_url_rejects_unsafe_values_at_config_and_protocol_boundaries(
    url,
    message,
):
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, tool_gateway_url=url)

    with pytest.raises(ValidationError, match=message):
        AgentRunRequest(
            run_id="run-gateway",
            task_id="task-1",
            task_input={},
            seed=1,
            environment_snapshot="cassette:v1",
            cassette_content_sha256="0" * 64,
            tool_grant_token="a" * 43,
            tool_gateway_url=url,
        )


def test_tool_gateway_url_is_normalized_at_config_and_protocol_boundaries():
    url = "https://gateway.test/api/v1/tools/invoke/"
    settings = Settings(_env_file=None, tool_gateway_url=url)
    request = AgentRunRequest(
        run_id="run-gateway",
        task_id="task-1",
        task_input={},
        seed=1,
        environment_snapshot="cassette:v1",
        cassette_content_sha256="0" * 64,
        tool_grant_token="a" * 43,
        tool_gateway_url=url,
    )

    assert settings.tool_gateway_url == "https://gateway.test/api/v1/tools/invoke"
    assert request.tool_gateway_url == settings.tool_gateway_url


@pytest.mark.parametrize(
    "environment_snapshot",
    ["customer-tools-v2", "customer-tools-v2:", ":cassette-v1", " a:v1", "a:v1:extra"],
)
def test_environment_snapshot_requires_cassette_id_and_version(environment_snapshot):
    with pytest.raises(ValidationError, match="<cassette-id>:<version>"):
        Settings(_env_file=None, environment_snapshot=environment_snapshot)

    with pytest.raises(ValidationError, match="<cassette-id>:<version>"):
        AgentRunRequest(
            run_id="run-gateway",
            task_id="task-1",
            task_input={},
            seed=1,
            environment_snapshot=environment_snapshot,
            cassette_content_sha256="0" * 64,
            tool_grant_token="a" * 43,
            tool_gateway_url="http://gateway.test",
        )


@pytest.mark.parametrize("digest", ["", "0" * 63, "G" * 64])
def test_agent_request_rejects_invalid_cassette_content_digest(digest):
    with pytest.raises(ValidationError):
        AgentRunRequest(
            run_id="run-gateway",
            task_id="task-1",
            task_input={},
            seed=1,
            environment_snapshot="cassette:v1",
            cassette_content_sha256=digest,
            tool_grant_token="a" * 43,
            tool_gateway_url="http://gateway.test",
        )



@pytest.mark.parametrize("token", ["", "a" * 31, "contains space" * 3])
def test_agent_request_rejects_invalid_tool_grant_token(token):
    with pytest.raises(ValidationError):
        AgentRunRequest(
            run_id="run-gateway",
            task_id="task-1",
            task_input={},
            seed=1,
            environment_snapshot="cassette:v1",
            cassette_content_sha256="0" * 64,
            tool_grant_token=token,
            tool_gateway_url="http://gateway.test",
        )


def test_tool_grant_maintenance_settings_are_bounded():
    settings = Settings(_env_file=None)
    assert settings.tool_grant_retention_seconds == 604800
    assert settings.tool_grant_cleanup_batch_size == 1000

    for value in (3599, 31_536_001):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, tool_grant_retention_seconds=value)
    for value in (0, 10_001):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, tool_grant_cleanup_batch_size=value)


def test_judge_timeout_is_bounded():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, judge_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, judge_timeout_seconds=601)


def test_judge_context_and_endpoint_are_bounded():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, judge_max_context_bytes=1023)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, judge_max_context_bytes=1_048_577)
    with pytest.raises(ValidationError, match="credentials"):
        Settings(
            _env_file=None,
            openai_base_url="https://user:secret@judge.test/v1",
        )
    settings = Settings(
        _env_file=None,
        openai_base_url="https://api.openai.test/v1/",
    )
    assert settings.openai_base_url == "https://api.openai.test/v1"


def test_deepseek_judge_runtime_selects_its_own_credentials_and_endpoint():
    settings = Settings(
        _env_file=None,
        judge_provider="deepseek",
        openai_api_key="openai-placeholder",
        deepseek_api_key="deepseek-placeholder",
        deepseek_base_url="https://api.deepseek.test/v1/",
        judge_model="deepseek-chat",
    )

    assert settings.judge_api_key == "deepseek-placeholder"
    assert settings.judge_base_url == "https://api.deepseek.test/v1"
    assert settings.judge_model == "deepseek-chat"


def test_candidate_override_rejects_endpoint_credentials_before_bootstrap():
    payload = candidate_payload("remote-v2")
    payload["endpoint"] = "https://token:secret@remote-agent.test"
    with pytest.raises(ValidationError, match="credentials"):
        Settings(
            _env_file=None,
            candidate_overrides={"remote-v2": payload},
        )
