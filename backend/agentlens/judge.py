from __future__ import annotations

import json
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from agentlens.config import get_settings


class JudgeVerdict(BaseModel):
    supported: bool
    confidence: Literal["low", "medium", "high"]
    category: str
    evidence_sequences: list[int] = Field(default_factory=list)
    explanation: str


async def semantic_failure_review(payload: dict[str, Any]) -> JudgeVerdict | None:
    """Optional calibrated semantic review through any OpenAI-compatible endpoint.

    `None` deliberately means the deterministic offline mode is active. Callers must
    never turn that absence into a positive verdict.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    response = await client.responses.parse(
        model=settings.judge_model,
        input=[
            {
                "role": "system",
                "content": (
                    "你是 Agent 轨迹失败复核员。只根据给定事件证据判断规则归因是否成立；"
                    "不要把参考轨迹视为唯一正确路径。输出结构化结论并引用事件序号。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text_format=JudgeVerdict,
    )
    return response.output_parsed

