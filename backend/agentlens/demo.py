from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from agentlens.config import BENCHMARK_NAME, get_settings
from agentlens.evaluation import (
    aggregate_runs,
    attribute_failures,
    canonical_hash,
    check_assertion,
    compute_cost,
    paired_bootstrap_difference,
    trajectory_match,
)
from agentlens.exceptions import ExperimentCancelled
from agentlens.judge import JudgeReviewError, semantic_calibration_report
from agentlens.orchestrator import run_evaluation_workflow
from agentlens.runtime import resolve_candidate, run_http_trial
from agentlens.schemas import (
    AssertionSpec,
    CandidateSpec,
    EventType,
    ExperimentSummary,
    FailureEvidence,
    PlanStep,
    RunResult,
    TaskSpec,
    ToolCall,
    TraceEvent,
)

CANDIDATES = {
    "support-v1.4": CandidateSpec(
        id="support-v1.4",
        name="客服Agent",
        version="v1.4",
        model="scripted-support-1",
        model_parameters={"temperature": 0.2},
        prompt_hash="sha256:74b1d8a4",
        scaffold_version="support-flow@1.4.0",
        tool_schema_hash="sha256:aa72c34f",
    ),
    "support-v1.3": CandidateSpec(
        id="support-v1.3",
        name="客服Agent",
        version="v1.3",
        model="scripted-support-1",
        model_parameters={"temperature": 0.2},
        prompt_hash="sha256:0987ef31",
        scaffold_version="support-flow@1.3.2",
        tool_schema_hash="sha256:aa72c34f",
    ),
}


def candidate_catalog() -> dict[str, CandidateSpec]:
    """Merge validated HTTP candidate metadata without mutating scripted fixtures."""
    return {**CANDIDATES, **get_settings().candidate_overrides}


TASKS = [
    TaskSpec(
        id="task-1",
        name="查询客户订单",
        input={"customer_id": "C-1042"},
        initial_state={"customer": None},
        allowed_tools=["search_customer", "list_orders"],
        assertions=[AssertionSpec(path="customer.id", expected="C-1042")],
        required_communication=["C-1042"],
        reference_trajectory=[
            ToolCall(name="search_customer", arguments={"customer_id": "C-1042"})
        ],
    ),
    TaskSpec(
        id="task-2",
        name="核验退款资格",
        input={"order_id": "O-8891"},
        initial_state={"eligible": None},
        allowed_tools=["get_order", "check_refund_policy"],
        assertions=[AssertionSpec(path="eligible", expected=True)],
        required_communication=["符合退款条件"],
        reference_trajectory=[
            ToolCall(name="get_order", arguments={"order_id": "O-8891"}),
            ToolCall(name="check_refund_policy", arguments={"order_id": "O-8891"}),
        ],
        trajectory_match_mode="subset",
    ),
    TaskSpec(
        id="task-3",
        name="创建售后工单",
        input={"order_id": "O-7790"},
        initial_state={"ticket": None},
        allowed_tools=["get_order", "create_ticket"],
        assertions=[AssertionSpec(path="ticket.status", expected="open")],
        required_communication=["工单"],
        reference_trajectory=[
            ToolCall(name="get_order", arguments={"order_id": "O-7790"}),
            ToolCall(name="create_ticket", arguments={"order_id": "O-7790"}),
        ],
    ),
    TaskSpec(
        id="task-4",
        name="修改收货地址",
        input={"order_id": "O-2207", "city": "上海"},
        initial_state={"address": {"city": "杭州"}},
        allowed_tools=["get_order", "update_address"],
        assertions=[AssertionSpec(path="address.city", expected="上海")],
        required_communication=["上海"],
        reference_trajectory=[
            ToolCall(name="get_order", arguments={"order_id": "O-2207"}),
            ToolCall(name="update_address", arguments={"order_id": "O-2207", "city": "上海"}),
        ],
        trajectory_required=True,
    ),
    TaskSpec(
        id="task-5",
        name="解释账单差异",
        input={"invoice_id": "I-5521"},
        initial_state={"verified": False},
        allowed_tools=["get_invoice", "get_payment", "verify_amount"],
        assertions=[AssertionSpec(path="verified", expected=True)],
        required_communication=["差额"],
        reference_trajectory=[
            ToolCall(name="get_invoice", arguments={"invoice_id": "I-5521"}),
            ToolCall(name="get_payment", arguments={"invoice_id": "I-5521"}),
            ToolCall(name="verify_amount", arguments={"invoice_id": "I-5521"}),
        ],
    ),
    TaskSpec(
        id="task-6",
        name="升级高优先级投诉",
        input={"customer_id": "C-9910"},
        initial_state={"escalation": None},
        allowed_tools=["search_customer", "classify_priority", "escalate_case"],
        assertions=[AssertionSpec(path="escalation.level", expected="high")],
        required_communication=["已升级"],
        reference_trajectory=[
            ToolCall(name="search_customer", arguments={"customer_id": "C-9910"}),
            ToolCall(name="classify_priority", arguments={"customer_id": "C-9910"}),
            ToolCall(name="escalate_case", arguments={"customer_id": "C-9910"}),
        ],
    ),
]


def _resolve_task_snapshot(
    task_snapshot: Sequence[TaskSpec] | None,
) -> tuple[TaskSpec, ...]:
    source = TASKS if task_snapshot is None else task_snapshot
    tasks = tuple(task.model_copy(deep=True) for task in source)
    task_ids = [task.id for task in tasks]
    if not tasks or any(not task_id.strip() for task_id in task_ids):
        raise ValueError("task snapshot must contain named tasks")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task snapshot IDs must be unique")
    return tasks


FAILURE_PROFILES = {
    "support-v1.4": {
        "task-1": {3: "loop"},
        "task-2": {5: "tool_misuse", 8: "premature_completion"},
        "task-3": {7: "context_loss"},
        "task-4": {2: "tool_misuse", 9: "premature_completion"},
        "task-5": {1: "unverified", 6: "loop", 10: "unverified"},
        "task-6": {4: "context_loss", 8: "premature_completion"},
    },
    "support-v1.3": {
        "task-1": {2: "loop", 5: "context_loss", 9: "premature_completion"},
        "task-2": {1: "tool_misuse", 4: "unverified", 8: "premature_completion"},
        "task-3": {3: "context_loss", 6: "tool_misuse", 10: "premature_completion"},
        "task-4": {2: "tool_misuse", 5: "context_loss", 7: "premature_completion", 9: "loop"},
        "task-5": {1: "unverified", 4: "loop", 6: "unverified", 8: "premature_completion"},
        "task-6": {2: "context_loss", 3: "tool_misuse", 7: "premature_completion", 10: "loop"},
    },
}


def _event(run_id: str, seq: int, event_type: EventType, **payload: Any) -> TraceEvent:
    timestamp = datetime(2026, 8, 12, 10, 21, tzinfo=UTC) + timedelta(seconds=seq)
    return TraceEvent(run_id=run_id, seq=seq, timestamp=timestamp, type=event_type, payload=payload)


def _success_state(task: TaskSpec) -> dict[str, Any]:
    states = {
        "task-1": {"customer": {"id": "C-1042", "name": "林晓"}},
        "task-2": {"eligible": True},
        "task-3": {"ticket": {"id": "T-2048", "status": "open"}},
        "task-4": {"address": {"city": "上海"}},
        "task-5": {"verified": True, "difference": 12.5},
        "task-6": {"escalation": {"level": "high", "id": "E-61"}},
    }
    return states[task.id]


def _answer(task: TaskSpec, success: bool) -> str:
    success_answers = {
        "task-1": "已找到客户 C-1042，共有 2 个订单。",
        "task-2": "订单符合退款条件，可以继续办理。",
        "task-3": "售后工单 T-2048 已创建。",
        "task-4": "收货地址已修改为上海。",
        "task-5": "账单差额为 ¥12.50，已经完成三方核验。",
        "task-6": "投诉已升级为高优先级，专员会立即处理。",
    }
    return success_answers[task.id] if success else "处理已经完成。"


def run_scripted_trial(
    candidate_id: str, task: TaskSpec, seed: int, run_namespace: str = "standalone"
) -> RunResult:
    # Run identity must be globally unique because events refer to it across experiments.
    run_id = f"{run_namespace}-{candidate_id}-{task.id}-{seed:02d}"
    failure = FAILURE_PROFILES[candidate_id].get(task.id, {}).get(seed)
    events = [_event(run_id, 0, EventType.RUN_STARTED, seed=seed, task_id=task.id)]
    seq = 1
    trajectory = [
        call.model_copy(update={"seq": index + 1})
        for index, call in enumerate(task.reference_trajectory)
    ]
    if failure == "tool_misuse":
        trajectory = [
            ToolCall(
                name="delete_customer",
                arguments={"customer_id": task.input.get("customer_id", "unknown")},
                seq=1,
            )
        ]
    elif failure == "loop":
        first = task.reference_trajectory[0]
        trajectory = [first.model_copy(update={"seq": index + 1}) for index in range(4)]
    elif failure == "context_loss":
        trajectory = task.reference_trajectory[:1]
    elif failure in {"premature_completion", "unverified"}:
        trajectory = task.reference_trajectory[:-1]

    for call in trajectory:
        valid = call.name in task.allowed_tools
        events.append(
            _event(
                run_id,
                seq,
                EventType.TOOL_CALL,
                name=call.name,
                arguments=call.arguments,
                valid_args=valid,
            )
        )
        seq += 1
        events.append(
            _event(
                run_id,
                seq,
                EventType.TOOL_RESULT,
                name=call.name,
                result={"ok": valid, "fixture": canonical_hash(call.arguments)},
            )
        )
        seq += 1
    input_tokens = 560 + seed * 17 + len(trajectory) * 83
    output_tokens = 94 + len(trajectory) * 21
    events.append(
        _event(
            run_id,
            seq,
            EventType.USAGE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=120,
        )
    )
    seq += 1

    success = failure is None
    final_state = _success_state(task) if success else task.initial_state
    answer = _answer(task, success)
    events.append(
        _event(
            run_id,
            seq,
            EventType.FINAL,
            answer=answer,
            verified=failure not in {"unverified", "loop"},
            declared_complete=True,
        )
    )
    seq += 1
    events.append(
        _event(run_id, seq, EventType.RUN_COMPLETED, status="success" if success else "failed")
    )
    outcome_ok = all(check_assertion(final_state, assertion) for assertion in task.assertions)
    communication_ok = all(fragment in answer for fragment in task.required_communication)
    trajectory_score = trajectory_match(
        trajectory, task.reference_trajectory, task.trajectory_match_mode
    )
    success = (
        outcome_ok
        and communication_ok
        and (trajectory_score == 1.0 if task.trajectory_required else True)
    )
    cost, cost_complete = compute_cost(events)
    failures = attribute_failures(events, task.allowed_tools, success)
    if failure == "context_loss":
        failures.insert(
            0,
            FailureEvidence(
                category="context_loss",
                label="上下文丢失",
                severity="high",
                event_range=(1, max(1, seq - 2)),
                rule="state.required_field_missing",
                explanation="后续步骤未携带前序已获取的关键标识。",
            ),
        )
    duration = round(17.0 + seed * 1.9 + len(trajectory) * 3.2, 1)
    return RunResult(
        run_id=run_id,
        task_id=task.id,
        candidate_id=candidate_id,
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
        duration_seconds=duration,
        failures=failures,
    )


def calibration_report() -> dict[str, Any]:
    samples = [
        {"id": f"cal-{index + 1:02d}", "expected": index % 3 != 0, "predicted": index % 3 != 0}
        for index in range(24)
    ]
    # Ensure two explicit disagreements and 22/24 accuracy.
    samples[7]["predicted"] = not samples[7]["expected"]
    samples[18]["predicted"] = not samples[18]["expected"]
    correct = sum(item["expected"] == item["predicted"] for item in samples)
    tp = sum(item["expected"] and item["predicted"] for item in samples)
    tn = sum(not item["expected"] and not item["predicted"] for item in samples)
    fp = sum(not item["expected"] and item["predicted"] for item in samples)
    fn = sum(item["expected"] and not item["predicted"] for item in samples)
    return {
        "total": 24,
        "correct": correct,
        "accuracy": correct / 24,
        "passed": correct / 24 >= 0.9,
        "confusion_matrix": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "disagreements": [item["id"] for item in samples if item["expected"] != item["predicted"]],
        "mode": "scripted_calibration_fixture",
    }


def build_demo_experiment(
    experiment_id: str,
    prompt: str,
    candidate_id: str = "support-v1.4",
    baseline_id: str | None = "support-v1.3",
    repetitions: int = 10,
    execution_mode: str = "scripted",
    candidate_snapshot: CandidateSpec | None = None,
    baseline_snapshot: CandidateSpec | None = None,
    environment_snapshot: str | None = None,
    cassette_content_sha256: str | None = None,
    tool_gateway_url: str | None = None,
    task_snapshot: Sequence[TaskSpec] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    benchmark_name: str = BENCHMARK_NAME,
) -> ExperimentSummary:
    if execution_mode not in {"scripted", "http"}:
        raise ValueError(f"unknown execution mode: {execution_mode}")
    tasks = _resolve_task_snapshot(task_snapshot)
    candidates = candidate_catalog() if execution_mode == "http" else CANDIDATES
    candidate = candidate_snapshot or candidates[candidate_id]
    baseline = baseline_snapshot
    if baseline is None and baseline_id is not None:
        baseline = candidates[baseline_id]
    if candidate.id != candidate_id or (
        (baseline.id if baseline else None) != baseline_id
    ):
        raise ValueError("candidate snapshots do not match requested ids")
    total_work = len(tasks) * repetitions * (2 if baseline else 1)
    candidate_runs: list[RunResult] = []
    baseline_runs: list[RunResult] = []
    workflow: dict[str, Any] = {}

    def snapshot_environment() -> None:
        nonlocal candidate, baseline
        if execution_mode == "http":
            candidate = resolve_candidate(candidate)
            if baseline is not None:
                baseline = resolve_candidate(baseline)
        workflow["environment_snapshot"] = environment_snapshot
        workflow["cassette_content_sha256"] = cassette_content_sha256

    def calibrate_judge() -> None:
        if execution_mode == "scripted":
            workflow["calibration"] = calibration_report()
            return
        try:
            semantic_report = asyncio.run(semantic_calibration_report())
        except JudgeReviewError as error:
            semantic_report = {
                "total": 0,
                "correct": 0,
                "accuracy": 0.0,
                "passed": False,
                "confusion_matrix": {"tp": 0, "tn": 0, "fp": 0, "fn": 0},
                "disagreements": [],
                "mode": error.category,
            }
        workflow["calibration"] = semantic_report or {
            "total": 0,
            "correct": 0,
            "accuracy": 0.0,
            "passed": False,
            "confusion_matrix": {"tp": 0, "tn": 0, "fp": 0, "fn": 0},
            "disagreements": [],
            "mode": "not_run",
        }

    def run_trials() -> None:
        completed_work = 0
        if on_progress is not None:
            on_progress(completed_work, total_work)
        targets = [(candidate_runs, candidate)]
        if baseline is not None:
            targets.append((baseline_runs, baseline))
        for target, current_candidate in targets:
            for task in tasks:
                for seed in range(1, repetitions + 1):
                    if is_cancelled is not None and is_cancelled():
                        raise ExperimentCancelled(experiment_id)
                    if execution_mode == "http":
                        target.append(
                            run_http_trial(
                                current_candidate,
                                task,
                                seed,
                                experiment_id,
                                is_cancelled=is_cancelled,
                                environment_snapshot=environment_snapshot,
                                cassette_content_sha256=cassette_content_sha256,
                                tool_gateway_url=tool_gateway_url,
                            )
                        )
                    else:
                        target.append(
                            run_scripted_trial(
                                current_candidate.id,
                                task,
                                seed,
                                experiment_id,
                            )
                        )
                    completed_work += 1
                    if on_progress is not None:
                        on_progress(completed_work, total_work)
        workflow["completed_work"] = completed_work

    def analyze_trajectories() -> None:
        workflow["candidate_metrics"] = aggregate_runs(candidate_runs)
        workflow["baseline_metrics"] = aggregate_runs(baseline_runs)
        workflow["candidate_outcomes"] = {
            task.id: {
                run.seed: run.success for run in candidate_runs if run.task_id == task.id
            }
            for task in tasks
        }
        workflow["baseline_outcomes"] = {
            task.id: {
                run.seed: run.success for run in baseline_runs if run.task_id == task.id
            }
            for task in tasks
        }

    def attribute_failure_summary() -> None:
        failures: dict[str, int] = {}
        for run in candidate_runs:
            if run.failures:
                label = run.failures[0].label
                failures[label] = failures.get(label, 0) + 1
        workflow["failures"] = failures
        workflow["critical_policy_violations"] = sum(
            item.category == "policy_violation" and item.severity == "critical"
            for run in candidate_runs
            for item in run.failures
        )

    def compare_candidates() -> None:
        if baseline is not None:
            workflow["comparison"] = paired_bootstrap_difference(
                workflow["candidate_outcomes"],
                workflow["baseline_outcomes"],
            )
        else:
            workflow["comparison"] = (0.0, 0.0, 0.0)

    def generate_report() -> None:
        candidate_metrics = workflow["candidate_metrics"]
        calibration = workflow["calibration"]
        low_interval = candidate_metrics["success_interval"][0]
        average_cost = candidate_metrics["average_cost_cny"]
        cost_ok = (
            candidate_metrics["cost_complete"]
            and average_cost is not None
            and average_cost <= 1.0
        )
        passed = (
            low_interval >= 0.70
            and workflow["critical_policy_violations"] == 0
            and cost_ok
            and calibration["passed"]
        )
        failed_reasons = []
        if low_interval < 0.70:
            failed_reasons.append("成功率区间下界未达到 70%")
        if workflow["critical_policy_violations"]:
            failed_reasons.append("存在关键策略违规")
        if not cost_ok:
            failed_reasons.append("成本数据不完整或超过门槛")
        if not calibration["passed"]:
            failed_reasons.append("裁判校准未达到门槛")
        workflow["passed"] = passed
        workflow["failed_reasons"] = failed_reasons

    run_evaluation_workflow(
        {
            "snapshot_environment": snapshot_environment,
            "calibrate_judge": calibrate_judge,
            "run_trials": run_trials,
            "analyze_trajectories": analyze_trajectories,
            "attribute_failures": attribute_failure_summary,
            "compare_candidates": compare_candidates,
            "generate_report": generate_report,
        }
    )
    completed_work = workflow["completed_work"]
    candidate_metrics = workflow["candidate_metrics"]
    baseline_metrics = workflow["baseline_metrics"]
    point, low, high = workflow["comparison"]
    calibration = workflow["calibration"]
    failures = workflow["failures"]
    passed = workflow["passed"]
    failed_reasons = workflow["failed_reasons"]
    return ExperimentSummary(
        id=experiment_id,
        status="completed",
        prompt=prompt,
        candidate=candidate,
        baseline=baseline,
        benchmark_name=benchmark_name,
        completed_runs=completed_work,
        total_runs=total_work,
        plan=[
            PlanStep(key="snapshot", label="锁定候选版本与环境快照", status="completed"),
            PlanStep(
                key="calibrate",
                label="校验裁判：24 个标注样本",
                status="completed",
                detail=f"一致率 {calibration['accuracy']:.1%}",
            ),
            PlanStep(
                key="trials",
                label=(
                    f"运行候选/基线各 {len(tasks)} 个任务 × {repetitions} 次"
                    if baseline
                    else f"运行候选 {len(tasks)} 个任务 × {repetitions} 次"
                ),
                status="completed",
                detail=f"{completed_work} / {total_work}",
            ),
            PlanStep(key="analyze", label="分析轨迹质量", status="completed"),
            PlanStep(key="attribute", label="执行失败归因", status="completed"),
            PlanStep(
                key="compare",
                label="对比候选版本",
                status="completed",
                detail="配对 bootstrap" if baseline else "未选择基线",
            ),
            PlanStep(
                key="report",
                label=(
                    f"生成 {baseline.version} 对比报告"
                    if baseline
                    else "生成单候选测评报告"
                ),
                status="completed",
            ),
        ],
        runs=candidate_runs,
        baseline_runs=baseline_runs,
        metrics=candidate_metrics,
        comparison={
            "baseline_metrics": baseline_metrics,
            "difference": point,
            "interval": (low, high),
            "significant": low > 0 or high < 0,
            "message": (
                (
                    f"{candidate.version} 显著优于 {baseline.version}"
                    if low > 0
                    else f"区间重叠，暂不判定 {candidate.version} 显著优于 {baseline.version}"
                )
                if baseline
                else "本次为单候选测评，未运行配对 A/B 统计。"
            ),
        },
        judge_calibration=calibration,
        failures=dict(sorted(failures.items(), key=lambda item: -item[1])),
        verdict={
            "passed": passed,
            "label": "达到上线门槛" if passed else "暂不建议上线",
            "reason": (
                "稳定性区间、关键违规、成本与裁判校准均满足任务集门槛。"
                if passed
                else "；".join(failed_reasons) + "。"
            ),
            "thresholds": {
                "success_interval_lower": 0.70,
                "critical_policy_violations": 0,
                "max_average_cost_cny": 1.0,
                "min_judge_accuracy": 0.90,
            },
        },
        created_at=datetime.now(UTC),
    )


def build_pending_experiment(
    experiment_id: str,
    request_prompt: str,
    candidate_id: str = "support-v1.4",
    baseline_id: str | None = "support-v1.3",
    repetitions: int = 10,
    execution_mode: str = "scripted",
    task_snapshot: Sequence[TaskSpec] | None = None,
    candidate_snapshot: CandidateSpec | None = None,
    baseline_snapshot: CandidateSpec | None = None,
    benchmark_name: str = BENCHMARK_NAME,
) -> ExperimentSummary:
    """Build a schema-complete queued response without fabricating evaluation results."""
    tasks = _resolve_task_snapshot(task_snapshot)
    total_work = len(tasks) * repetitions * (2 if baseline_id else 1)
    candidates = candidate_catalog() if execution_mode == "http" else CANDIDATES
    candidate = candidate_snapshot or candidates[candidate_id]
    baseline = baseline_snapshot
    if baseline is None and baseline_id is not None:
        baseline = candidates[baseline_id]
    if execution_mode == "http":
        candidate = resolve_candidate(candidate)
        if baseline is not None:
            baseline = resolve_candidate(baseline)
    return ExperimentSummary(
        id=experiment_id,
        status="queued",
        prompt=request_prompt,
        candidate=candidate,
        baseline=baseline,
        benchmark_name=benchmark_name,
        completed_runs=0,
        total_runs=total_work,
        plan=[
            PlanStep(key="snapshot", label="锁定候选版本与环境快照", status="queued"),
            PlanStep(key="calibrate", label="校验裁判：24 个标注样本", status="queued"),
            PlanStep(
                key="trials",
                label=(
                    f"运行候选/基线各 {len(tasks)} 个任务 × {repetitions} 次"
                    if baseline
                    else f"运行候选 {len(tasks)} 个任务 × {repetitions} 次"
                ),
                status="queued",
                detail=f"0 / {total_work}",
            ),
            PlanStep(key="analyze", label="分析轨迹质量", status="queued"),
            PlanStep(key="attribute", label="执行失败归因", status="queued"),
            PlanStep(
                key="compare",
                label="对比候选版本",
                status="queued",
                detail=None if baseline else "未选择基线",
            ),
            PlanStep(
                key="report",
                label=(
                    f"生成 {baseline.version} 对比报告"
                    if baseline
                    else "生成单候选测评报告"
                ),
                status="queued",
            ),
        ],
        runs=[],
        baseline_runs=[],
        metrics={
            "success_rate": 0.0,
            "success_interval": (0.0, 0.0),
            "average_cost_cny": None,
            "cost_complete": False,
            "average_tool_calls": 0.0,
            "average_trajectory_score": 0.0,
            "by_task": {},
        },
        comparison={
            "baseline_metrics": {},
            "difference": 0.0,
            "interval": (0.0, 0.0),
            "significant": False,
            "message": "等待评测完成。",
        },
        judge_calibration={
            "total": 0,
            "correct": 0,
            "accuracy": 0.0,
            "passed": False,
            "confusion_matrix": {"tp": 0, "tn": 0, "fp": 0, "fn": 0},
            "disagreements": [],
            "mode": "not_run",
        },
        failures={},
        verdict={
            "passed": False,
            "label": "等待评测",
            "reason": "实验尚未完成，不能给出上线结论。",
            "thresholds": {
                "success_interval_lower": 0.70,
                "critical_policy_violations": 0,
                "max_average_cost_cny": 1.0,
                "min_judge_accuracy": 0.90,
            },
        },
        created_at=datetime.now(UTC),
    )
