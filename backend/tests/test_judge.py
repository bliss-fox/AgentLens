import json
from types import SimpleNamespace

import pytest

import agentlens.judge as judge_module
from agentlens.calibration_cases import CALIBRATION_CASES
from agentlens.judge import (
    JudgeReviewError,
    JudgeVerdict,
    apply_judge_review,
    semantic_calibration_report,
    semantic_failure_review,
)
from agentlens.schemas import FailureEvidence


@pytest.mark.asyncio
async def test_semantic_review_stays_offline_without_api_key(monkeypatch):
    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key=None),
    )

    class UnexpectedClient:
        def __init__(self, **kwargs):
            raise AssertionError("offline mode created an OpenAI client")

    monkeypatch.setattr(judge_module, "AsyncOpenAI", UnexpectedClient)

    assert await semantic_failure_review({"failure": "fixture"}) is None


@pytest.mark.asyncio
async def test_semantic_review_applies_timeout_and_closes_client(monkeypatch):
    state = {"closed": False, "client_kwargs": None, "request_kwargs": None}
    verdict = JudgeVerdict(
        supported=True,
        confidence="high",
        category="tool_misuse",
        evidence_sequences=[2, 3],
        explanation="The cited tool call violates the task policy.",
    )

    class FakeResponses:
        async def parse(self, **kwargs):
            state["request_kwargs"] = kwargs
            return SimpleNamespace(output_parsed=verdict)

    class FakeClient:
        def __init__(self, **kwargs):
            state["client_kwargs"] = kwargs
            self.responses = FakeResponses()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            state["closed"] = True

    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-placeholder",
            openai_base_url="https://api.openai.test/v1",
            judge_model="judge-model",
            judge_timeout_seconds=17.5,
            judge_max_context_bytes=131_072,
        ),
    )
    monkeypatch.setattr(judge_module, "AsyncOpenAI", FakeClient)

    result = await semantic_failure_review(
        {
            "failure": "fixture",
            "events": [
                {
                    "seq": 2,
                    "payload": {
                        "authorization": "Bearer private-token",
                        "nested": {"api_key": "private-key", "safe": "visible"},
                    },
                }
            ],
        }
    )

    assert result == verdict
    assert state["closed"] is True
    assert state["client_kwargs"] == {
        "api_key": "test-placeholder",
        "base_url": "https://api.openai.test/v1",
        "timeout": 17.5,
        "max_retries": 0,
    }
    assert state["request_kwargs"]["model"] == "judge-model"
    assert state["request_kwargs"]["text_format"] is JudgeVerdict
    sent_payload = json.loads(state["request_kwargs"]["input"][1]["content"])
    assert sent_payload["events"][0]["payload"]["authorization"] == "[REDACTED]"
    assert sent_payload["events"][0]["payload"]["nested"] == {
        "api_key": "[REDACTED]",
        "safe": "visible",
    }


@pytest.mark.asyncio
async def test_semantic_review_sanitizes_timeout_and_closes_client(monkeypatch):
    state = {"closed": False}

    class FailedResponses:
        async def parse(self, **kwargs):
            raise TimeoutError("https://user:secret@judge.test")

    class FailedClient:
        def __init__(self, **kwargs):
            self.responses = FailedResponses()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            state["closed"] = True

    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-placeholder",
            openai_base_url="https://api.openai.test/v1",
            judge_model="judge-model",
            judge_timeout_seconds=1.0,
            judge_max_context_bytes=131_072,
        ),
    )
    monkeypatch.setattr(judge_module, "AsyncOpenAI", FailedClient)

    with pytest.raises(JudgeReviewError) as captured:
        await semantic_failure_review({"failure": "fixture"})

    assert captured.value.category == "judge_timeout"
    assert captured.value.retryable is True
    assert str(captured.value) == "semantic judge request timed out"
    assert "secret" not in str(captured.value)
    assert state["closed"] is True


@pytest.mark.asyncio
async def test_semantic_review_rejects_oversized_context_before_client(monkeypatch):
    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-placeholder",
            judge_max_context_bytes=32,
        ),
    )

    class UnexpectedClient:
        def __init__(self, **kwargs):
            raise AssertionError("oversized context created an OpenAI client")

    monkeypatch.setattr(judge_module, "AsyncOpenAI", UnexpectedClient)

    with pytest.raises(JudgeReviewError) as captured:
        await semantic_failure_review({"events": [{"payload": {"text": "x" * 100}}]})

    assert captured.value.category == "judge_context_too_large"
    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_semantic_review_rejects_invalid_parsed_verdict(monkeypatch):
    state = {"closed": False}

    class InvalidResponses:
        async def parse(self, **kwargs):
            return SimpleNamespace(output_parsed={"supported": True})

    class InvalidClient:
        def __init__(self, **kwargs):
            self.responses = InvalidResponses()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            state["closed"] = True

    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-placeholder",
            openai_base_url="https://api.openai.test/v1",
            judge_model="judge-model",
            judge_timeout_seconds=1.0,
            judge_max_context_bytes=131_072,
        ),
    )
    monkeypatch.setattr(judge_module, "AsyncOpenAI", InvalidClient)

    with pytest.raises(JudgeReviewError) as captured:
        await semantic_failure_review({"failure": "fixture"})

    assert captured.value.category == "judge_invalid_response"
    assert captured.value.retryable is False
    assert state["closed"] is True


@pytest.mark.asyncio
async def test_deepseek_semantic_review_uses_chat_json_and_closes_client(monkeypatch):
    state = {"closed": False, "request": None}
    verdict = {
        "supported": True,
        "confidence": "high",
        "category": "tool_misuse",
        "evidence_sequences": [2],
        "explanation": "事件 2 调用了未授权工具。",
    }

    class FakeCompletions:
        async def create(self, **kwargs):
            state["request"] = kwargs
            message = SimpleNamespace(content=json.dumps(verdict, ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            state["closed"] = True

    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(
            judge_api_key="deepseek-placeholder",
            judge_base_url="https://api.deepseek.test/v1",
            judge_provider="deepseek",
            openai_api_key=None,
            openai_base_url="https://api.openai.test/v1",
            judge_model="deepseek-chat",
            judge_timeout_seconds=10.0,
            judge_max_context_bytes=131_072,
        ),
    )
    monkeypatch.setattr(judge_module, "AsyncOpenAI", FakeClient)

    result = await semantic_failure_review({"events": [{"seq": 2}]})

    assert result == JudgeVerdict.model_validate(verdict)
    assert state["closed"] is True
    assert state["request"]["response_format"] == {"type": "json_object"}
    assert state["request"]["model"] == "deepseek-chat"


def test_semantic_calibration_dataset_has_24_balanced_human_annotations():
    assert len(CALIBRATION_CASES) == 24
    assert len({item["id"] for item in CALIBRATION_CASES}) == 24
    assert sum(item["expected_supported"] for item in CALIBRATION_CASES) == 12
    assert all(item["human_annotation"] for item in CALIBRATION_CASES)


@pytest.mark.asyncio
async def test_semantic_calibration_reports_real_model_accuracy(monkeypatch):
    predictions = [
        {"id": item["id"], "supported": item["expected_supported"]}
        for item in CALIBRATION_CASES
    ]

    class FakeCompletions:
        async def create(self, **kwargs):
            content = json.dumps({"predictions": predictions})
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(
            judge_api_key="deepseek-placeholder",
            judge_base_url="https://api.deepseek.test/v1",
            judge_provider="deepseek",
            openai_api_key=None,
            openai_base_url="https://api.openai.test/v1",
            judge_model="deepseek-chat",
            judge_timeout_seconds=10.0,
        ),
    )
    monkeypatch.setattr(judge_module, "AsyncOpenAI", FakeClient)

    report = await semantic_calibration_report()

    assert report is not None
    assert report["total"] == 24
    assert report["correct"] == 24
    assert report["accuracy"] == 1.0
    assert report["passed"] is True
    assert report["mode"] == "semantic_model_calibration"


def test_judge_review_requires_real_citations_and_preserves_audit_details():
    failure = FailureEvidence(
        category="tool_misuse",
        label="工具误用",
        severity="high",
        event_range=(2, 3),
        rule="tool.allowlist_or_schema",
        explanation="deterministic explanation",
    )
    verdict = JudgeVerdict(
        supported=True,
        confidence="medium",
        category="tool_misuse",
        evidence_sequences=[3, 2, 3],
        explanation="semantic explanation",
    )

    reviewed = apply_judge_review(failure, verdict, {0, 1, 2, 3, 4})

    assert reviewed.judge_verdict == "support"
    assert reviewed.confidence == "medium"
    assert reviewed.judge_explanation == "semantic explanation"
    assert reviewed.judge_evidence_sequences == [2, 3]
    assert reviewed.explanation == "deterministic explanation"

    with pytest.raises(ValueError, match="outside the attributed event range"):
        apply_judge_review(
            failure,
            verdict.model_copy(update={"evidence_sequences": [4]}),
            {0, 1, 2, 3, 4},
        )

    conflict = apply_judge_review(
        failure,
        verdict.model_copy(update={"category": "context_loss"}),
        {0, 1, 2, 3, 4},
    )
    assert conflict.judge_verdict == "conflict"
