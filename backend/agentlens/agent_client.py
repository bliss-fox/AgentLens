from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass

import httpx

from agentlens.pricing import compute_usage_cost_cny
from agentlens.schemas import (
    AgentRunRequest,
    Budget,
    EventStreamValidator,
    EventType,
    TraceEvent,
    Usage,
)


class AgentProtocolError(RuntimeError):
    def __init__(self, message: str, category: str = "protocol_error") -> None:
        super().__init__(message)
        self.category = category


@dataclass
class RunBudgetTracker:
    budget: Budget
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def accept(self, event: TraceEvent) -> None:
        if event.type == EventType.TOOL_CALL:
            self.tool_calls += 1
        elif event.type == EventType.USAGE:
            usage = Usage.model_validate(event.payload)
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.cached_tokens += usage.cached_tokens

        if self.tool_calls > self.budget.max_tool_calls:
            raise AgentProtocolError("agent exceeded the tool-call budget", "budget_exhausted")
        if self.input_tokens + self.output_tokens > self.budget.max_tokens:
            raise AgentProtocolError("agent exceeded the token budget", "budget_exhausted")

        cost_cny = compute_usage_cost_cny(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
            tool_calls=self.tool_calls,
        )
        if cost_cny > self.budget.max_cost_cny:
            raise AgentProtocolError("agent exceeded the cost budget", "budget_exhausted")


async def parse_sse_lines(
    lines: AsyncIterator[str],
    run_id: str,
    budget: Budget | None = None,
) -> list[TraceEvent]:
    """Parse an SSE stream and enforce event names, ordering, payloads, and budgets."""
    validator = EventStreamValidator(run_id)
    limits = budget or Budget()
    budget_tracker = RunBudgetTracker(limits)
    events: list[TraceEvent] = []
    data_lines: list[str] = []
    data_bytes = 0
    stream_bytes = 0
    event_name: str | None = None

    def consume_data() -> None:
        nonlocal data_bytes, event_name
        if len(events) >= limits.max_events:
            raise AgentProtocolError("agent exceeded the event-count budget", "budget_exhausted")
        if event_name != "trace":
            raise AgentProtocolError("each agent event must use the SSE event name 'trace'")
        try:
            event = TraceEvent.model_validate_json("\n".join(data_lines))
            validator.accept(event)
            budget_tracker.accept(event)
        except AgentProtocolError:
            raise
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentProtocolError(str(error)) from error
        events.append(event)
        data_lines.clear()
        data_bytes = 0
        event_name = None

    async for raw_line in lines:
        stream_bytes += len(raw_line.encode("utf-8"))
        if stream_bytes > limits.max_stream_bytes:
            raise AgentProtocolError("agent exceeded the stream-size budget", "budget_exhausted")
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines:
                consume_data()
            else:
                event_name = None
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:].lstrip()
            data_bytes += len(value.encode("utf-8"))
            if data_bytes > limits.max_event_bytes:
                raise AgentProtocolError("agent exceeded the event-size budget", "budget_exhausted")
            data_lines.append(value)
    if data_lines:
        consume_data()
    try:
        validator.finish()
    except ValueError as error:
        raise AgentProtocolError(str(error)) from error
    return events


async def run_http_agent(
    endpoint: str,
    request: AgentRunRequest,
    transport: httpx.AsyncBaseTransport | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cancellation_poll_seconds: float = 0.05,
) -> list[TraceEvent]:
    """Run one remote agent stream with wall-clock budget and cooperative cancellation."""
    timeout = httpx.Timeout(None, connect=min(10, request.budget.max_seconds))

    async def request_events() -> list[TraceEvent]:
        async with (
            httpx.AsyncClient(timeout=timeout, transport=transport) as client,
            client.stream(
                "POST",
                f"{endpoint.rstrip('/')}/v1/agent-runs",
                json=request.model_dump(mode="json"),
                headers={"Accept": "text/event-stream"},
            ) as response,
        ):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            media_type = content_type.partition(";")[0].strip().lower()
            if media_type != "text/event-stream":
                raise AgentProtocolError(f"expected text/event-stream, received {content_type}")
            return await parse_sse_lines(
                response.aiter_lines(),
                request.run_id,
                request.budget,
            )

    task: asyncio.Task[list[TraceEvent]] | None = None
    try:
        async with asyncio.timeout(request.budget.max_seconds):
            task = asyncio.create_task(request_events())
            if is_cancelled is None:
                return await task
            poll_seconds = max(0.01, cancellation_poll_seconds)
            while True:
                done, _ = await asyncio.wait({task}, timeout=poll_seconds)
                if done:
                    return task.result()
                if await asyncio.to_thread(is_cancelled):
                    raise AgentProtocolError("agent run was cancelled", "cancelled")
    except (TimeoutError, httpx.TimeoutException) as error:
        raise AgentProtocolError("agent run exceeded its wall-clock timeout", "timeout") from error
    except httpx.HTTPStatusError as error:
        raise AgentProtocolError(
            f"agent endpoint returned HTTP {error.response.status_code}", "agent_http_error"
        ) from error
    except httpx.RequestError as error:
        raise AgentProtocolError(
            "agent endpoint could not be reached", "agent_connection_error"
        ) from error
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
