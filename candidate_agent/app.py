from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("agentlens.candidate_agent")

SYSTEM_PROMPT = """You are CodingAssistant, an expert AI pair programmer.
Help users write, understand, debug, and improve Python code. Be concise, precise,
and educational. Use analyze_code for static inspection. Use execute_python only
when the user asks to run code or when execution is needed to verify a proposed fix.
Before execution, explain that you are about to run code. The runner is constrained,
has no supported network or file access, and is not a sandbox for hostile code.
Never claim a task was verified unless the relevant tool result succeeded."""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": "Execute Python in a constrained runner and return structured output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_code",
            "description": "Parse Python and return deterministic static-analysis findings.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    },
]


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


PROMPT_HASH = _sha256_json({"system_prompt": SYSTEM_PROMPT})
TOOL_SCHEMA_HASH = _sha256_json(TOOL_SCHEMAS)
SCAFFOLD_VERSION = "agentlens-openai-compat@0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    llm_provider: Literal["openai", "deepseek"] = "openai"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = ""
    model_timeout_seconds: int = Field(default=120, ge=1, le=600)
    candidate_fake_model: bool = False
    llm_input_price_cny_per_million: float | None = Field(default=None, ge=0)
    llm_output_price_cny_per_million: float | None = Field(default=None, ge=0)
    llm_cached_price_cny_per_million: float | None = Field(default=None, ge=0)
    llm_tool_cost_cny: float = Field(default=0, ge=0)

    @property
    def ready(self) -> bool:
        return self.candidate_fake_model or bool(self.api_key)

    @property
    def api_key(self) -> str:
        value = (
            self.deepseek_api_key
            if self.llm_provider == "deepseek"
            else self.openai_api_key
        )
        return value.strip()

    @property
    def model_base_url(self) -> str:
        return (
            self.deepseek_base_url
            if self.llm_provider == "deepseek"
            else self.openai_base_url
        )

    @property
    def resolved_model(self) -> str:
        return self.llm_model.strip() or (
            "deepseek-chat" if self.llm_provider == "deepseek" else "gpt-4o"
        )

    @property
    def missing_key_name(self) -> str:
        return (
            "DEEPSEEK_API_KEY"
            if self.llm_provider == "deepseek"
            else "OPENAI_API_KEY"
        )

    @property
    def pricing_cny(self) -> dict[str, float | str]:
        defaults = (
            {
                "input_per_million": 2.0,
                "output_per_million": 8.0,
                "cached_per_million": 0.5,
            }
            if self.llm_provider == "deepseek"
            else {
                "input_per_million": 14.0,
                "output_per_million": 56.0,
                "cached_per_million": 3.5,
            }
        )
        return {
            "version": "adapter-config@2026-09-14",
            "input_per_million": (
                self.llm_input_price_cny_per_million
                if self.llm_input_price_cny_per_million is not None
                else defaults["input_per_million"]
            ),
            "output_per_million": (
                self.llm_output_price_cny_per_million
                if self.llm_output_price_cny_per_million is not None
                else defaults["output_per_million"]
            ),
            "cached_per_million": (
                self.llm_cached_price_cny_per_million
                if self.llm_cached_price_cny_per_million is not None
                else defaults["cached_per_million"]
            ),
            "tool_cost": self.llm_tool_cost_cny,
        }


class Budget(BaseModel):
    max_seconds: int = Field(default=180, ge=1, le=3600)
    max_tokens: int = Field(default=20_000, ge=1)
    max_tool_calls: int = Field(default=30, ge=0)
    max_events: int = Field(default=1000, ge=3)
    max_event_bytes: int = Field(default=262_144, ge=256)
    max_stream_bytes: int = Field(default=4_194_304, ge=1024)
    max_cost_cny: float = Field(default=5.0, ge=0)


class AgentRunRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=120)
    task_input: dict[str, Any]
    seed: int
    environment_snapshot: str
    cassette_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_grant_token: str = Field(pattern=r"^[A-Za-z0-9_-]{32,200}$")
    tool_gateway_url: str
    budget: Budget = Field(default_factory=Budget)

    @field_validator("tool_gateway_url")
    @classmethod
    def validate_gateway(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("tool_gateway_url must be HTTP(S)")
        return value.rstrip("/")


class ModelToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ModelTurn(BaseModel):
    text: str = ""
    tool_calls: list[ModelToolCall] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class ModelAdapter(Protocol):
    async def complete(self, messages: list[dict[str, Any]], seed: int) -> ModelTurn: ...


class OpenAIModelAdapter:
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.model_base_url,
            timeout=settings.model_timeout_seconds,
        )
        self._model = settings.resolved_model
        self._provider = settings.llm_provider

    async def complete(self, messages: list[dict[str, Any]], seed: int) -> ModelTurn:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
        }
        # OpenAI supports deterministic seeds. DeepSeek's OpenAI-compatible
        # chat endpoint does not document this field, so omitting it prevents
        # a provider-side 400 while the platform still records the trial seed.
        if self._provider == "openai":
            request["seed"] = seed
        response = await self._client.chat.completions.create(**request)
        choice = response.choices[0].message
        calls: list[ModelToolCall] = []
        for call in choice.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError as error:
                raise ValueError("model returned invalid tool arguments") from error
            if not isinstance(arguments, dict):
                raise TypeError("model tool arguments must be an object")
            calls.append(
                ModelToolCall(id=call.id, name=call.function.name, arguments=arguments)
            )
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        return ModelTurn(
            text=choice.content or "",
            tool_calls=calls,
            input_tokens=int(usage.prompt_tokens if usage else 0),
            output_tokens=int(usage.completion_tokens if usage else 0),
            cached_tokens=cached,
        )


class FakeModelAdapter:
    """Deterministic adapter for tests and Compose contract checks only."""

    async def complete(self, messages: list[dict[str, Any]], seed: int) -> ModelTurn:
        if any(item.get("role") == "tool" for item in messages):
            return ModelTurn(text="工具验证完成。", input_tokens=20, output_tokens=8)
        text = str(messages[-1].get("content", ""))
        if "静态" in text or "analyze_code" in text:
            return ModelTurn(
                tool_calls=[
                    ModelToolCall(
                        id="fake-analysis",
                        name="analyze_code",
                        arguments={"code": "try:\n    pass\nexcept:\n    pass"},
                    )
                ],
                input_tokens=20,
                output_tokens=8,
            )
        return ModelTurn(text="任务已完成。", input_tokens=20, output_tokens=8)


def _event(
    run_id: str,
    seq: int,
    event_type: str,
    payload: dict[str, Any],
    *,
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "seq": seq,
        "timestamp": datetime.now(UTC).isoformat(),
        "type": event_type,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "payload": payload,
    }


def _sse(event: dict[str, Any]) -> bytes:
    return (
        "event: trace\ndata: "
        + json.dumps(event, ensure_ascii=False)
        + "\n\n"
    ).encode("utf-8")


def _task_message(task_input: dict[str, Any]) -> str:
    for key in ("message", "prompt", "instruction"):
        value = task_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(task_input, ensure_ascii=False, sort_keys=True)


async def _invoke_tool(
    client: httpx.AsyncClient,
    request: AgentRunRequest,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    cassette_id = request.environment_snapshot.split(":", 1)[0]
    response = await client.post(
        request.tool_gateway_url,
        json={
            "run_id": request.run_id,
            "tool_grant_token": request.tool_grant_token,
            "cassette_id": cassette_id,
            "cassette_content_sha256": request.cassette_content_sha256,
            "mode": "replay",
            "tool": name,
            "arguments": arguments,
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"tool gateway rejected {name} with HTTP {response.status_code}"
        )
    result = response.json().get("result")
    if not isinstance(result, dict):
        raise TypeError("tool gateway returned an invalid result")
    return result


async def stream_run(
    body: AgentRunRequest,
    model: ModelAdapter,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[bytes]:
    seq = 0
    yield _sse(
        _event(
            body.run_id,
            seq,
            "run.started",
            {"seed": body.seed, "task_id": body.task_id},
        )
    )
    seq += 1
    message = _task_message(body.task_input)
    yield _sse(
        _event(
            body.run_id,
            seq,
            "message",
            {"role": "user", "content": message},
        )
    )
    seq += 1
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    tool_results: dict[str, Any] = {}
    final_text = ""
    try:
        async with httpx.AsyncClient(
            timeout=min(body.budget.max_seconds, 30), transport=transport
        ) as client:
            for _ in range(body.budget.max_tool_calls + 1):
                turn = await model.complete(messages, body.seed)
                yield _sse(
                    _event(
                        body.run_id,
                        seq,
                        "usage",
                        turn.model_dump(
                            include={"input_tokens", "output_tokens", "cached_tokens"}
                        ),
                    )
                )
                seq += 1
                if not turn.tool_calls:
                    final_text = turn.text
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.text or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(
                                        call.arguments, ensure_ascii=False
                                    ),
                                },
                            }
                            for call in turn.tool_calls
                        ],
                    }
                )
                for call in turn.tool_calls:
                    yield _sse(
                        _event(
                            body.run_id,
                            seq,
                            "tool.call",
                            {"name": call.name, "arguments": call.arguments},
                            span_id=call.id,
                        )
                    )
                    seq += 1
                    result = await _invoke_tool(
                        client, body, call.name, call.arguments
                    )
                    tool_results[call.name] = result
                    yield _sse(
                        _event(
                            body.run_id,
                            seq,
                            "tool.result",
                            {"name": call.name, "result": result},
                            span_id=f"{call.id}-result",
                            parent_span_id=call.id,
                        )
                    )
                    seq += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                yield _sse(
                    _event(
                        body.run_id,
                        seq,
                        "state",
                        {"state": {"tool_results": tool_results}},
                    )
                )
                seq += 1
            else:
                raise RuntimeError("model exceeded the tool-call loop limit")
        yield _sse(
            _event(
                body.run_id,
                seq,
                "final",
                {
                    "answer": final_text,
                    "verified": bool(tool_results),
                    "final_state": {"tool_results": tool_results},
                },
            )
        )
        seq += 1
        yield _sse(
            _event(body.run_id, seq, "run.completed", {"status": "success"})
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("candidate run failed: run_id=%s", body.run_id)
        yield _sse(
            _event(
                body.run_id,
                seq,
                "error",
                {"category": "candidate_error", "message": "candidate run failed"},
            )
        )
        seq += 1
        yield _sse(
            _event(body.run_id, seq, "run.completed", {"status": "failed"})
        )


def create_app(model: ModelAdapter | None = None) -> FastAPI:
    application = FastAPI(
        title="AgentLens coding candidate adapter", version="0.1.0"
    )
    settings = Settings()

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    async def ready() -> JSONResponse:
        if not settings.ready and model is None:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "detail": f"{settings.missing_key_name} is missing",
                },
                status_code=503,
            )
        selected_model = (
            "fake"
            if model is not None or settings.candidate_fake_model
            else settings.resolved_model
        )
        provider = "fake" if selected_model == "fake" else settings.llm_provider
        return JSONResponse(
            {
                "status": "ready",
                "provider": provider,
                "model": selected_model,
                "identity": {
                    "schema": "agentlens.candidate-identity/v1",
                    "model": selected_model,
                    "model_parameters": {
                        "provider": provider,
                        "tool_choice": "auto",
                        "seed_parameter": provider == "openai",
                        "pricing_cny": settings.pricing_cny,
                    },
                    "prompt_hash": PROMPT_HASH,
                    "scaffold_version": SCAFFOLD_VERSION,
                    "tool_schema_hash": TOOL_SCHEMA_HASH,
                },
            }
        )

    @application.post("/v1/agent-runs", response_model=None)
    async def agent_runs(
        body: AgentRunRequest, request: Request
    ) -> StreamingResponse | JSONResponse:
        if not settings.ready and model is None:
            return JSONResponse(
                {"detail": f"{settings.missing_key_name} is missing"},
                status_code=503,
            )
        selected = model or (
            FakeModelAdapter()
            if settings.candidate_fake_model
            else OpenAIModelAdapter(settings)
        )

        async def guarded_stream() -> AsyncIterator[bytes]:
            async for chunk in stream_run(body, selected):
                if await request.is_disconnected():
                    return
                yield chunk

        return StreamingResponse(
            guarded_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return application


app = create_app()
