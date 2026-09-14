import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from agentlens.agent_client import AgentProtocolError, run_http_agent
from agentlens.cassette import parse_environment_snapshot
from agentlens.config import TOOL_GRANT_EXPIRY_GRACE_SECONDS, get_settings
from agentlens.evaluation import attribute_failures, check_assertion, compute_cost, trajectory_match
from agentlens.exceptions import ExperimentCancelled
from agentlens.schemas import (
    AgentRunRequest,
    CandidateSpec,
    EventType,
    RunResult,
    TaskSpec,
    ToolCall,
    TraceEvent,
    normalize_http_url,
)
from agentlens.tool_gateway import registry
from agentlens.tool_grant_repository import issue_tool_grant, revoke_tool_grant


def resolve_candidate(candidate: CandidateSpec) -> CandidateSpec:
    endpoint = candidate.endpoint or get_settings().agent_endpoints.get(candidate.id)
    if not endpoint:
        raise ValueError(
            f"HTTP execution requires an endpoint for candidate {candidate.id}; "
            "configure AGENT_ENDPOINTS as a JSON object"
        )
    endpoint = normalize_http_url(str(endpoint), f"candidate {candidate.id} endpoint")
    return candidate.model_copy(update={"endpoint": endpoint})


def _state_from_events(task: TaskSpec, events: list[TraceEvent]) -> dict:
    state = task.initial_state
    for event in events:
        if event.type == EventType.STATE:
            value = event.payload.get("state", event.payload)
            if isinstance(value, dict):
                state = value
        elif event.type == EventType.FINAL:
            value = event.payload.get("final_state")
            if isinstance(value, dict):
                state = value
    return state


def _result_from_events(
    candidate: CandidateSpec,
    task: TaskSpec,
    seed: int,
    run_id: str,
    events: list[TraceEvent],
    duration_seconds: float,
) -> RunResult:
    final_events = [event for event in events if event.type == EventType.FINAL]
    completed_events = [event for event in events if event.type == EventType.RUN_COMPLETED]
    final = final_events[-1] if final_events else None
    completed = completed_events[-1] if completed_events else None
    answer = str(final.payload.get("answer", "")) if final else ""
    final_state = _state_from_events(task, events)
    trajectory = [
        ToolCall(
            name=str(event.payload.get("name", "")),
            arguments=event.payload.get("arguments", {}),
            seq=event.seq,
        )
        for event in events
        if event.type == EventType.TOOL_CALL
    ]
    trajectory_score = trajectory_match(
        trajectory, task.reference_trajectory, task.trajectory_match_mode
    )
    outcome_ok = all(check_assertion(final_state, assertion) for assertion in task.assertions)
    communication_ok = all(fragment in answer for fragment in task.required_communication)
    runtime_ok = (
        final is not None
        and completed is not None
        and completed.payload.get("status") in {"success", "completed"}
        and not any(event.type == EventType.ERROR for event in events)
    )
    success = (
        runtime_ok
        and outcome_ok
        and communication_ok
        and (trajectory_score == 1.0 if task.trajectory_required else True)
    )
    cost, cost_complete = compute_cost(
        events,
        model_parameters=candidate.model_parameters,
    )
    failures = attribute_failures(events, task.allowed_tools, success)
    return RunResult(
        run_id=run_id,
        task_id=task.id,
        candidate_id=candidate.id,
        seed=seed,
        success=success,
        final_answer=answer,
        final_state=final_state,
        events=events,
        trajectory=trajectory,
        trajectory_score=trajectory_score,
        cost_cny=cost,
        cost_complete=cost_complete,
        tool_calls=len(trajectory),
        duration_seconds=round(duration_seconds, 3),
        failures=failures,
    )


def _error_events(run_id: str, task_id: str, seed: int, error: AgentProtocolError):
    category = error.category
    if category in {"agent_http_error", "agent_connection_error"}:
        category = "environment_error"
    public_messages = {
        "protocol_error": "agent event stream violated the trace protocol",
        "budget_exhausted": "agent exceeded the configured run budget",
        "timeout": "agent run exceeded its wall-clock timeout",
        "environment_error": "agent endpoint was unavailable",
    }
    message = public_messages.get(category, "agent run failed")
    now = datetime.now(UTC)
    return [
        TraceEvent(
            run_id=run_id,
            seq=0,
            timestamp=now,
            type=EventType.RUN_STARTED,
            payload={"seed": seed, "task_id": task_id},
        ),
        TraceEvent(
            run_id=run_id,
            seq=1,
            timestamp=now,
            type=EventType.ERROR,
            payload={"category": category, "message": message},
        ),
        TraceEvent(
            run_id=run_id,
            seq=2,
            timestamp=now,
            type=EventType.RUN_COMPLETED,
            payload={"status": "failed"},
        ),
    ]


def run_http_trial(
    candidate: CandidateSpec,
    task: TaskSpec,
    seed: int,
    run_namespace: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    environment_snapshot: str | None = None,
    cassette_content_sha256: str | None = None,
    tool_gateway_url: str | None = None,
) -> RunResult:
    candidate = resolve_candidate(candidate)
    run_id = f"{run_namespace}-{candidate.id}-{task.id}-{seed:02d}"
    resolved_environment_snapshot = environment_snapshot or get_settings().environment_snapshot
    resolved_cassette_sha256 = cassette_content_sha256
    if resolved_cassette_sha256 is None:
        contract = registry.environment_contract(resolved_environment_snapshot)
        resolved_cassette_sha256 = contract["cassette"]["content_sha256"]
    cassette_id, _ = parse_environment_snapshot(resolved_environment_snapshot)
    grant_token = issue_tool_grant(
        run_id=run_id,
        experiment_id=run_namespace,
        cassette_id=cassette_id,
        cassette_content_sha256=resolved_cassette_sha256,
        allowed_tools=task.allowed_tools,
        max_calls=task.budget.max_tool_calls,
        ttl_seconds=task.budget.max_seconds + TOOL_GRANT_EXPIRY_GRACE_SECONDS,
    )
    try:
        request = AgentRunRequest(
            run_id=run_id,
            task_id=task.id,
            task_input=task.input,
            seed=seed,
            environment_snapshot=resolved_environment_snapshot,
            cassette_content_sha256=resolved_cassette_sha256,
            tool_grant_token=grant_token,
            tool_gateway_url=tool_gateway_url or get_settings().tool_gateway_url,
            budget=task.budget,
        )
        started = time.perf_counter()
        try:
            events = asyncio.run(
                run_http_agent(
                    candidate.endpoint or "",
                    request,
                    transport=transport,
                    is_cancelled=is_cancelled,
                )
            )
        except AgentProtocolError as error:
            if error.category == "cancelled":
                raise ExperimentCancelled(run_id) from error
            events = _error_events(run_id, task.id, seed, error)
        return _result_from_events(
            candidate,
            task,
            seed,
            run_id,
            events,
            time.perf_counter() - started,
        )
    finally:
        if not revoke_tool_grant(grant_token):
            raise RuntimeError("tool grant disappeared before revocation")
