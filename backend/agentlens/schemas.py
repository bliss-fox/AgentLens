from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    MESSAGE = "message"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    STATE = "state"
    USAGE = "usage"
    FINAL = "final"
    ERROR = "error"
    RUN_COMPLETED = "run.completed"


class TraceEvent(BaseModel):
    run_id: str
    seq: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: EventType
    span_id: str | None = None
    parent_span_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class Budget(BaseModel):
    max_seconds: int = 180
    max_tokens: int = 20_000
    max_tool_calls: int = 30
    max_cost_cny: float = 5.0


class AgentRunRequest(BaseModel):
    run_id: str
    task_id: str
    task_input: dict[str, Any]
    seed: int
    environment_snapshot: str
    tool_gateway_url: str
    budget: Budget = Field(default_factory=Budget)


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    seq: int = 0


class AssertionSpec(BaseModel):
    path: str
    op: Literal["eq", "contains", "exists", "not_exists"] = "eq"
    expected: Any = None


class AcceptanceThresholds(BaseModel):
    success_interval_lower: float = 0.70
    critical_policy_violations: int = 0
    max_average_cost_cny: float = 1.0
    min_judge_accuracy: float = 0.90


class TaskSpec(BaseModel):
    id: str
    name: str
    input: dict[str, Any]
    initial_state: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str]
    assertions: list[AssertionSpec] = Field(default_factory=list)
    required_communication: list[str] = Field(default_factory=list)
    natural_language_assertions: list[str] = Field(default_factory=list)
    reference_trajectory: list[ToolCall] = Field(default_factory=list)
    trajectory_match_mode: Literal["strict", "unordered", "subset", "superset"] = "subset"
    trajectory_required: bool = False
    repetitions: int = Field(default=10, ge=1, le=100)
    budget: Budget = Field(default_factory=Budget)
    thresholds: AcceptanceThresholds = Field(default_factory=AcceptanceThresholds)


class CandidateSpec(BaseModel):
    id: str
    name: str
    version: str
    model: str
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    prompt_hash: str
    scaffold_version: str
    tool_schema_hash: str
    endpoint: str | None = None


class FailureEvidence(BaseModel):
    category: str
    label: str
    severity: Literal["low", "medium", "high", "critical"]
    event_range: tuple[int, int]
    rule: str
    explanation: str
    judge_verdict: Literal["support", "conflict", "not_run"] = "not_run"
    confidence: Literal["low", "medium", "high"] = "high"


class RunResult(BaseModel):
    run_id: str
    task_id: str
    candidate_id: str
    seed: int
    success: bool
    final_answer: str
    final_state: dict[str, Any]
    events: list[TraceEvent]
    trajectory: list[ToolCall]
    trajectory_score: float
    cost_cny: float | None
    cost_complete: bool
    tool_calls: int
    duration_seconds: float
    failures: list[FailureEvidence] = Field(default_factory=list)


class ExperimentRequest(BaseModel):
    prompt: str
    candidate_id: str = "support-v1.4"
    baseline_candidate_id: str = "support-v1.3"
    benchmark_id: str = "customer-tools-v2"
    repetitions: int = Field(default=10, ge=1, le=50)


class PlanStep(BaseModel):
    key: str
    label: str
    status: Literal["completed", "active", "queued", "failed"]
    detail: str | None = None


class ExperimentSummary(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "cancelled", "failed"]
    prompt: str
    candidate: CandidateSpec
    baseline: CandidateSpec
    benchmark_name: str
    completed_runs: int
    total_runs: int
    plan: list[PlanStep]
    runs: list[RunResult]
    metrics: dict[str, Any]
    comparison: dict[str, Any]
    judge_calibration: dict[str, Any]
    failures: dict[str, int]
    verdict: dict[str, Any]
    created_at: datetime


class EventStreamValidator:
    """Strictly validates an agent's monotonically ordered SSE event stream."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.next_seq = 0
        self.started = False
        self.terminal = False
        self.saw_final_or_error = False

    def accept(self, event: TraceEvent) -> None:
        if event.run_id != self.run_id:
            raise ValueError("event run_id does not match request")
        if self.terminal:
            raise ValueError("event received after terminal event")
        if event.seq != self.next_seq:
            raise ValueError(f"expected sequence {self.next_seq}, received {event.seq}")
        if not self.started and event.type != EventType.RUN_STARTED:
            raise ValueError("first event must be run.started")
        if event.type == EventType.RUN_STARTED:
            if self.started:
                raise ValueError("duplicate run.started event")
            self.started = True
        if event.type in {EventType.FINAL, EventType.ERROR}:
            self.saw_final_or_error = True
        if event.type == EventType.RUN_COMPLETED:
            if not self.saw_final_or_error:
                raise ValueError("run.completed requires final or error event")
            self.terminal = True
        self.next_seq += 1

    def finish(self) -> None:
        if not self.terminal:
            raise ValueError("stream ended without run.completed")
