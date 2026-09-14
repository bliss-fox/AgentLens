from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from openai import APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from agentlens.calibration_cases import CALIBRATION_CASES, SEMANTIC_CALIBRATION_VERSION
from agentlens.config import get_settings
from agentlens.schemas import FailureEvidence


class JudgeVerdict(BaseModel):
    supported: bool
    confidence: Literal["low", "medium", "high"]
    category: str = Field(min_length=1, max_length=80)
    evidence_sequences: list[Annotated[int, Field(ge=0)]] = Field(
        min_length=1,
        max_length=100,
    )
    explanation: str = Field(min_length=1, max_length=4_000)

    @field_validator("category", "explanation")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("judge text fields must not be blank")
        return value.strip()


class CalibrationPrediction(BaseModel):
    id: str = Field(pattern=r"^cal-\d{2}$")
    supported: bool


class CalibrationBatch(BaseModel):
    predictions: list[CalibrationPrediction] = Field(min_length=24, max_length=24)


_SENSITIVE_JUDGE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "passwd",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
        "x_api_key",
    }
)


def sanitize_judge_payload(value: Any) -> Any:
    """Redact credential-shaped fields while preserving reviewable evidence."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized_key = (
                key.casefold().replace("-", "_") if isinstance(key, str) else ""
            )
            sanitized[key] = (
                "[REDACTED]"
                if normalized_key in _SENSITIVE_JUDGE_KEYS
                else sanitize_judge_payload(item)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_judge_payload(item) for item in value]
    return value


class JudgeReviewError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.category = category
        self.public_message = message
        self.retryable = retryable


def apply_judge_review(
    failure: FailureEvidence,
    verdict: JudgeVerdict,
    available_sequences: set[int],
) -> FailureEvidence:
    """Validate semantic citations and attach an auditable, non-destructive review."""
    citations = sorted(set(verdict.evidence_sequences))
    if not citations:
        raise ValueError("judge verdict must cite at least one event")
    if any(sequence not in available_sequences for sequence in citations):
        raise ValueError("judge citation is not present in the persisted trace")
    start, end = failure.event_range
    if any(sequence < start or sequence > end for sequence in citations):
        raise ValueError("judge citation is outside the attributed event range")
    judge_verdict = (
        "support"
        if verdict.supported is True and verdict.category == failure.category
        else "conflict"
    )
    return failure.model_copy(
        update={
            "judge_verdict": judge_verdict,
            "confidence": verdict.confidence,
            "judge_explanation": verdict.explanation,
            "judge_evidence_sequences": citations,
        }
    )


async def semantic_failure_review(payload: dict[str, Any]) -> JudgeVerdict | None:
    """Optional calibrated semantic review through any OpenAI-compatible endpoint.

    `None` deliberately means the deterministic offline mode is active. Callers must
    never turn that absence into a positive verdict.
    """
    settings = get_settings()
    api_key = getattr(settings, "judge_api_key", None) or getattr(
        settings, "openai_api_key", None
    )
    if not api_key:
        return None
    try:
        serialized_payload = json.dumps(
            sanitize_judge_payload(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise JudgeReviewError(
            "judge_invalid_request",
            "semantic judge context is not JSON serializable",
            retryable=False,
        ) from error
    if len(serialized_payload.encode("utf-8")) > settings.judge_max_context_bytes:
        raise JudgeReviewError(
            "judge_context_too_large",
            "semantic judge context exceeds configured size limit",
            retryable=False,
        )
    provider = getattr(settings, "judge_provider", "openai")
    base_url = getattr(settings, "judge_base_url", None) or settings.openai_base_url
    try:
        async with AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=settings.judge_timeout_seconds,
            max_retries=0,
        ) as client:
            if provider == "deepseek":
                response = await client.chat.completions.create(
                    model=settings.judge_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是 Agent 轨迹失败复核员。只根据给定事件证据判断规则归因是否成立；"
                                "不要把参考轨迹视为唯一正确路径。只能引用输入中提供的事件序号。"
                                "必须输出 JSON，字段为 supported、confidence、category、"
                                "evidence_sequences、explanation。"
                            ),
                        },
                        {"role": "user", "content": serialized_payload},
                    ],
                    response_format={"type": "json_object"},
                )
                raw_verdict = response.choices[0].message.content
            else:
                response = await client.responses.parse(
                    model=settings.judge_model,
                    input=[
                        {
                            "role": "system",
                            "content": (
                                "你是 Agent 轨迹失败复核员。只根据给定事件证据判断规则归因是否成立；"
                                "不要把参考轨迹视为唯一正确路径。只能引用输入中提供的事件序号。"
                            ),
                        },
                        {"role": "user", "content": serialized_payload},
                    ],
                    text_format=JudgeVerdict,
                )
                raw_verdict = response.output_parsed
    except (APITimeoutError, TimeoutError) as error:
        raise JudgeReviewError(
            "judge_timeout",
            "semantic judge request timed out",
            retryable=True,
        ) from error
    except Exception as error:
        raise JudgeReviewError(
            "judge_unavailable",
            "semantic judge request failed",
            retryable=True,
        ) from error
    try:
        return (
            JudgeVerdict.model_validate_json(raw_verdict)
            if isinstance(raw_verdict, str)
            else JudgeVerdict.model_validate(raw_verdict)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise JudgeReviewError(
            "judge_invalid_response",
            "semantic judge returned no valid structured verdict",
            retryable=False,
        ) from error


async def semantic_calibration_report() -> dict[str, Any] | None:
    settings = get_settings()
    api_key = getattr(settings, "judge_api_key", None) or getattr(
        settings, "openai_api_key", None
    )
    if not api_key:
        return None
    base_url = getattr(settings, "judge_base_url", None) or settings.openai_base_url
    cases = [
        {
            "id": item["id"],
            "proposed_category": item["proposed_category"],
            "evidence": item["evidence"],
        }
        for item in CALIBRATION_CASES
    ]
    payload = json.dumps({"cases": cases}, ensure_ascii=False, separators=(",", ":"))
    try:
        async with AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=settings.judge_timeout_seconds,
            max_retries=0,
        ) as client:
            response = await client.chat.completions.create(
                model=settings.judge_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 Agent 失败归因校准员。逐条判断 evidence 是否支持 proposed_category。"
                            "必须输出 JSON：{\"predictions\":[{\"id\":\"cal-01\","
                            "\"supported\":true}, ...]}，且恰好覆盖输入的 24 个 id。"
                        ),
                    },
                    {"role": "user", "content": payload},
                ],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
    except (APITimeoutError, TimeoutError) as error:
        raise JudgeReviewError(
            "judge_timeout",
            "semantic judge calibration timed out",
            retryable=True,
        ) from error
    except Exception as error:
        raise JudgeReviewError(
            "judge_unavailable",
            "semantic judge calibration failed",
            retryable=True,
        ) from error
    try:
        batch = CalibrationBatch.model_validate_json(content)
        predictions = {item.id: item.supported for item in batch.predictions}
        expected_ids = {item["id"] for item in CALIBRATION_CASES}
        if set(predictions) != expected_ids or len(predictions) != len(batch.predictions):
            raise ValueError("calibration response ids are missing or duplicated")
    except (TypeError, ValueError) as error:
        raise JudgeReviewError(
            "judge_invalid_response",
            "semantic judge calibration returned invalid JSON",
            retryable=False,
        ) from error
    expected = {item["id"]: item["expected_supported"] for item in CALIBRATION_CASES}
    correct = sum(predictions[item_id] == value for item_id, value in expected.items())
    tp = sum(expected[item_id] and predictions[item_id] for item_id in expected)
    tn = sum(not expected[item_id] and not predictions[item_id] for item_id in expected)
    fp = sum(not expected[item_id] and predictions[item_id] for item_id in expected)
    fn = sum(expected[item_id] and not predictions[item_id] for item_id in expected)
    return {
        "total": len(expected),
        "correct": correct,
        "accuracy": correct / len(expected),
        "passed": correct / len(expected) >= 0.9,
        "confusion_matrix": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "disagreements": [
            item_id for item_id, value in expected.items() if predictions[item_id] != value
        ],
        "mode": "semantic_model_calibration",
        "version": SEMANTIC_CALIBRATION_VERSION,
        "model": settings.judge_model,
        "provider": getattr(settings, "judge_provider", "openai"),
    }
