from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request

from agentlens.application_dependencies import cassette_registry_for
from agentlens.cassette import CassetteMissError
from agentlens.config import get_settings
from agentlens.exceptions import ToolAuthorizationError
from agentlens.tool_gateway import ToolInvocation
from agentlens.tool_grant_repository import consume_tool_grant

router = APIRouter(tags=["tools"])
DETERMINISTIC_RUNNER_TOOLS = frozenset({"analyze_code", "execute_python"})


@router.post("/api/v1/tools/invoke")
async def invoke_tool(invocation: ToolInvocation, request: Request) -> dict:
    cassette_registry = cassette_registry_for(request.app)
    try:
        consume_tool_grant(
            token=invocation.tool_grant_token,
            run_id=invocation.run_id,
            cassette_id=invocation.cassette_id,
            cassette_content_sha256=invocation.cassette_content_sha256,
            tool=invocation.tool,
        )
        if invocation.tool in DETERMINISTIC_RUNNER_TOOLS:
            settings = get_settings()
            try:
                async with httpx.AsyncClient(
                    timeout=settings.tool_runner_timeout_seconds
                ) as client:
                    response = await client.post(
                        f"{settings.tool_runner_url}/invoke",
                        json={
                            "tool": invocation.tool,
                            "arguments": invocation.arguments,
                        },
                    )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise TypeError("tool runner returned a non-object result")
                return {"result": result, "source": "constrained_runner"}
            except (httpx.HTTPError, TypeError, ValueError) as error:
                raise HTTPException(
                    424,
                    {
                        "category": "environment_error",
                        "message": "constrained tool runner is unavailable",
                    },
                ) from error
        return {
            "result": cassette_registry.invoke(invocation),
            "source": "frozen_cassette",
        }
    except ToolAuthorizationError as error:
        raise HTTPException(
            error.status_code,
            {
                "category": error.category,
                "message": error.public_message,
                "retryable": False,
            },
        ) from error
    except CassetteMissError as error:
        raise HTTPException(
            424, {"category": "environment_error", "message": str(error)}
        ) from error


@router.get("/api/v1/cassettes/{cassette_id}")
def cassette_snapshot(cassette_id: str, request: Request) -> dict:
    cassette_registry = cassette_registry_for(request.app)
    try:
        return cassette_registry.snapshot(cassette_id)
    except KeyError as error:
        raise HTTPException(404, "cassette not found") from error
