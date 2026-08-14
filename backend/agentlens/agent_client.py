from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from agentlens.schemas import AgentRunRequest, EventStreamValidator, TraceEvent


class AgentProtocolError(RuntimeError):
    def __init__(self, message: str, category: str = "protocol_error") -> None:
        super().__init__(message)
        self.category = category


async def parse_sse_lines(lines: AsyncIterator[str], run_id: str) -> list[TraceEvent]:
    """Parse arbitrarily chunked SSE lines and enforce the AgentLens trace contract."""
    validator = EventStreamValidator(run_id)
    events: list[TraceEvent] = []
    data_lines: list[str] = []

    def consume_data() -> None:
        try:
            event = TraceEvent.model_validate_json("\n".join(data_lines))
            validator.accept(event)
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentProtocolError(str(error)) from error
        events.append(event)
        data_lines.clear()

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines:
                consume_data()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
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
) -> list[TraceEvent]:
    timeout = httpx.Timeout(request.budget.max_seconds + 5, connect=10)
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client, client.stream(
            "POST", f"{endpoint.rstrip('/')}/v1/agent-runs", json=request.model_dump(mode="json"),
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                raise AgentProtocolError(f"expected text/event-stream, received {content_type}")
            return await parse_sse_lines(response.aiter_lines(), request.run_id)
    except httpx.TimeoutException as error:
        raise AgentProtocolError("agent run exceeded its HTTP timeout", "timeout") from error
    except httpx.HTTPStatusError as error:
        raise AgentProtocolError(
            f"agent endpoint returned HTTP {error.response.status_code}", "agent_http_error"
        ) from error
