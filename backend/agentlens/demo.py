from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agentlens.evaluation import (
    aggregate_runs,
    attribute_failures,
    canonical_hash,
    check_assertion,
    compute_cost,
    paired_bootstrap_difference,
    trajectory_match,
)
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
        id="support-v1.4", name="客服Agent", version="v1.4", model="scripted-support-1",
        model_parameters={"temperature": 0.2}, prompt_hash="sha256:74b1d8a4",
        scaffold_version="support-flow@1.4.0", tool_schema_hash="sha256:aa72c34f",
    ),
    "support-v1.3": CandidateSpec(
        id="support-v1.3", name="客服Agent", version="v1.3", model="scripted-support-1",
        model_parameters={"temperature": 0.2}, prompt_hash="sha256:0987ef31",
        scaffold_version="support-flow@1.3.2", tool_schema_hash="sha256:aa72c34f",
    ),
}


TASKS = [
    TaskSpec(
        id="task-1", name="查询客户订单", input={"customer_id": "C-1042"},
        initial_state={"customer": None}, allowed_tools=["search_customer", "list_orders"],
        assertions=[AssertionSpec(path="customer.id", expected="C-1042")],
        required_communication=["C-1042"],
        reference_trajectory=[ToolCall(name="search_customer", arguments={"customer_id": "C-1042"})],
    ),
    TaskSpec(
        id="task-2", name="核验退款资格", input={"order_id": "O-8891"},
        initial_state={"eligible": None}, allowed_tools=["get_order", "check_refund_policy"],
        assertions=[AssertionSpec(path="eligible", expected=True)], required_communication=["符合退款条件"],
        reference_trajectory=[ToolCall(name="get_order", arguments={"order_id": "O-8891"}), ToolCall(name="check_refund_policy", arguments={"order_id": "O-8891"})],
        trajectory_match_mode="subset",
    ),
    TaskSpec(
        id="task-3", name="创建售后工单", input={"order_id": "O-7790"},
        initial_state={"ticket": None}, allowed_tools=["get_order", "create_ticket"],
        assertions=[AssertionSpec(path="ticket.status", expected="open")], required_communication=["工单"],
        reference_trajectory=[ToolCall(name="get_order", arguments={"order_id": "O-7790"}), ToolCall(name="create_ticket", arguments={"order_id": "O-7790"})],
    ),
    TaskSpec(
        id="task-4", name="修改收货地址", input={"order_id": "O-2207", "city": "上海"},
        initial_state={"address": {"city": "杭州"}}, allowed_tools=["get_order", "update_address"],
        assertions=[AssertionSpec(path="address.city", expected="上海")], required_communication=["上海"],
        reference_trajectory=[ToolCall(name="get_order", arguments={"order_id": "O-2207"}), ToolCall(name="update_address", arguments={"order_id": "O-2207", "city": "上海"})],
        trajectory_required=True,
    ),
    TaskSpec(
        id="task-5", name="解释账单差异", input={"invoice_id": "I-5521"},
        initial_state={"verified": False}, allowed_tools=["get_invoice", "get_payment", "verify_amount"],
        assertions=[AssertionSpec(path="verified", expected=True)], required_communication=["差额"],
        reference_trajectory=[ToolCall(name="get_invoice", arguments={"invoice_id": "I-5521"}), ToolCall(name="get_payment", arguments={"invoice_id": "I-5521"}), ToolCall(name="verify_amount", arguments={"invoice_id": "I-5521"})],
    ),
    TaskSpec(
        id="task-6", name="升级高优先级投诉", input={"customer_id": "C-9910"},
        initial_state={"escalation": None}, allowed_tools=["search_customer", "classify_priority", "escalate_case"],
        assertions=[AssertionSpec(path="escalation.level", expected="high")], required_communication=["已升级"],
        reference_trajectory=[ToolCall(name="search_customer", arguments={"customer_id": "C-9910"}), ToolCall(name="classify_priority", arguments={"customer_id": "C-9910"}), ToolCall(name="escalate_case", arguments={"customer_id": "C-9910"})],
    ),
]


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
    trajectory = [call.model_copy(update={"seq": index + 1}) for index, call in enumerate(task.reference_trajectory)]
    if failure == "tool_misuse":
        trajectory = [ToolCall(name="delete_customer", arguments={"customer_id": task.input.get("customer_id", "unknown")}, seq=1)]
    elif failure == "loop":
        first = task.reference_trajectory[0]
        trajectory = [first.model_copy(update={"seq": index + 1}) for index in range(4)]
    elif failure == "context_loss":
        trajectory = task.reference_trajectory[:1]
    elif failure in {"premature_completion", "unverified"}:
        trajectory = task.reference_trajectory[:-1]

    for call in trajectory:
        valid = call.name in task.allowed_tools
        events.append(_event(run_id, seq, EventType.TOOL_CALL, name=call.name, arguments=call.arguments, valid_args=valid))
        seq += 1
        events.append(_event(run_id, seq, EventType.TOOL_RESULT, name=call.name, result={"ok": valid, "fixture": canonical_hash(call.arguments)}))
        seq += 1
    input_tokens = 560 + seed * 17 + len(trajectory) * 83
    output_tokens = 94 + len(trajectory) * 21
    events.append(_event(run_id, seq, EventType.USAGE, input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=120))
    seq += 1

    success = failure is None
    final_state = _success_state(task) if success else task.initial_state
    answer = _answer(task, success)
    events.append(_event(
        run_id, seq, EventType.FINAL, answer=answer, verified=failure not in {"unverified", "loop"},
        declared_complete=True,
    ))
    seq += 1
    events.append(_event(run_id, seq, EventType.RUN_COMPLETED, status="success" if success else "failed"))
    outcome_ok = all(check_assertion(final_state, assertion) for assertion in task.assertions)
    communication_ok = all(fragment in answer for fragment in task.required_communication)
    trajectory_score = trajectory_match(trajectory, task.reference_trajectory, task.trajectory_match_mode)
    success = outcome_ok and communication_ok and (trajectory_score == 1.0 if task.trajectory_required else True)
    cost, cost_complete = compute_cost(events)
    failures = attribute_failures(events, task.allowed_tools, success)
    if failure == "context_loss":
        failures.insert(0, FailureEvidence(
            category="context_loss", label="上下文丢失", severity="high", event_range=(1, max(1, seq - 2)),
            rule="state.required_field_missing", explanation="后续步骤未携带前序已获取的关键标识。", judge_verdict="support"
        ))
    duration = round(17.0 + seed * 1.9 + len(trajectory) * 3.2, 1)
    return RunResult(
        run_id=run_id, task_id=task.id, candidate_id=candidate_id, seed=seed, success=success,
        final_answer=answer, final_state=final_state, events=events, trajectory=trajectory,
        trajectory_score=trajectory_score, cost_cny=cost, cost_complete=cost_complete,
        tool_calls=len(trajectory), duration_seconds=duration, failures=failures,
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
        "total": 24, "correct": correct, "accuracy": correct / 24, "passed": correct / 24 >= 0.9,
        "confusion_matrix": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "disagreements": [item["id"] for item in samples if item["expected"] != item["predicted"]],
        "mode": "deterministic_offline",
    }


def build_demo_experiment(
    experiment_id: str, prompt: str, candidate_id: str = "support-v1.4",
    baseline_id: str = "support-v1.3", repetitions: int = 10,
) -> ExperimentSummary:
    candidate_runs = [
        run_scripted_trial(candidate_id, task, seed, experiment_id)
        for task in TASKS
        for seed in range(1, repetitions + 1)
    ]
    baseline_runs = [
        run_scripted_trial(baseline_id, task, seed, experiment_id)
        for task in TASKS
        for seed in range(1, repetitions + 1)
    ]
    candidate_metrics = aggregate_runs(candidate_runs)
    baseline_metrics = aggregate_runs(baseline_runs)
    candidate_outcomes = {task.id: [run.success for run in candidate_runs if run.task_id == task.id] for task in TASKS}
    baseline_outcomes = {task.id: [run.success for run in baseline_runs if run.task_id == task.id] for task in TASKS}
    point, low, high = paired_bootstrap_difference(candidate_outcomes, baseline_outcomes)
    calibration = calibration_report()
    failures: dict[str, int] = {}
    for run in candidate_runs:
        if run.failures:
            label = run.failures[0].label
            failures[label] = failures.get(label, 0) + 1
    low_interval = candidate_metrics["success_interval"][0]
    critical = sum(item.severity == "critical" for run in candidate_runs for item in run.failures)
    cost_ok = (candidate_metrics["average_cost_cny"] or 99) <= 1.0
    passed = low_interval >= 0.70 and critical == 0 and cost_ok and calibration["passed"]
    return ExperimentSummary(
        id=experiment_id, status="completed", prompt=prompt, candidate=CANDIDATES[candidate_id],
        baseline=CANDIDATES[baseline_id], benchmark_name="客服工具任务集 / v2",
        completed_runs=len(candidate_runs), total_runs=len(candidate_runs),
        plan=[
            PlanStep(key="snapshot", label="锁定候选版本与环境快照", status="completed"),
            PlanStep(key="calibrate", label="校验裁判：24 个标注样本", status="completed", detail=f"一致率 {calibration['accuracy']:.1%}"),
            PlanStep(key="trials", label=f"运行 6 个任务 × {repetitions} 次", status="completed", detail=f"{len(candidate_runs)} / {len(candidate_runs)}"),
            PlanStep(key="attribute", label="分析轨迹与失败归因", status="completed"),
            PlanStep(key="report", label=f"生成 {CANDIDATES[baseline_id].version} 对比报告", status="completed"),
        ],
        runs=candidate_runs, metrics=candidate_metrics,
        comparison={
            "baseline_metrics": baseline_metrics, "difference": point, "interval": (low, high),
            "significant": low > 0 or high < 0,
            "message": "v1.4 显著优于 v1.3" if low > 0 else "区间重叠，暂不判定 v1.4 显著优于 v1.3",
        },
        judge_calibration=calibration, failures=dict(sorted(failures.items(), key=lambda item: -item[1])),
        verdict={
            "passed": passed, "label": "达到上线门槛" if passed else "暂不建议上线",
            "reason": "稳定性区间、关键违规、成本与裁判校准均满足任务集门槛。" if passed else "成功率区间下界尚未达到 70%，建议优先修复循环调用和过早完成。",
            "thresholds": {"success_interval_lower": 0.70, "critical_policy_violations": 0, "max_average_cost_cny": 1.0, "min_judge_accuracy": 0.90},
        },
        created_at=datetime.now(UTC),
    )
