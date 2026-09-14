from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

import numpy as np

from agentlens.pricing import candidate_pricing, compute_usage_cost_cny
from agentlens.schemas import AssertionSpec, FailureEvidence, RunResult, ToolCall, TraceEvent, Usage


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def wilson_interval(
    successes: int, total: int, z: float = 1.959963984540054
) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator)


def stratified_bootstrap_interval(
    outcomes: dict[str, list[bool]], samples: int = 1_500, seed: int = 2026
) -> tuple[float, float]:
    if not outcomes:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    task_values = [np.asarray(values, dtype=float) for values in outcomes.values() if values]
    resampled_means = [
        values[rng.integers(0, len(values), size=(samples, len(values)))].mean(axis=1)
        for values in task_values
    ]
    estimates = np.vstack(resampled_means).mean(axis=0)
    low, high = np.quantile(estimates, [0.025, 0.975])
    return float(low), float(high)


def paired_bootstrap_difference(
    candidate: dict[str, list[bool] | dict[int, bool]],
    baseline: dict[str, list[bool] | dict[int, bool]],
    samples: int = 1_500,
    seed: int = 2026,
) -> tuple[float, float, float]:
    """Bootstrap paired outcomes after explicitly aligning each task by seed."""
    rng = np.random.default_rng(seed)
    pairs: list[tuple[np.ndarray, np.ndarray]] = []
    for task_id in sorted(set(candidate) & set(baseline)):
        candidate_by_seed = (
            candidate[task_id]
            if isinstance(candidate[task_id], dict)
            else dict(enumerate(candidate[task_id]))
        )
        baseline_by_seed = (
            baseline[task_id]
            if isinstance(baseline[task_id], dict)
            else dict(enumerate(baseline[task_id]))
        )
        shared_seeds = sorted(set(candidate_by_seed) & set(baseline_by_seed))
        if shared_seeds:
            left = np.asarray([candidate_by_seed[item] for item in shared_seeds], dtype=float)
            right = np.asarray([baseline_by_seed[item] for item in shared_seeds], dtype=float)
            pairs.append((left, right))
    if not pairs:
        return 0.0, 0.0, 0.0
    point = float(np.mean([left.mean() - right.mean() for left, right in pairs]))
    sampled_diffs = []
    for left, right in pairs:
        indices = rng.integers(0, len(left), size=(samples, len(left)))
        sampled_diffs.append((left[indices] - right[indices]).mean(axis=1))
    estimates = np.vstack(sampled_diffs).mean(axis=0)
    low, high = np.quantile(estimates, [0.025, 0.975])
    return point, float(low), float(high)


def _call_key(call: ToolCall, args_mode: str = "exact") -> tuple[str, str]:
    arguments = call.arguments if args_mode == "exact" else sorted(call.arguments)
    return call.name, json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def trajectory_match(actual: list[ToolCall], reference: list[ToolCall], mode: str) -> float:
    if not reference:
        return 1.0
    actual_keys = [_call_key(call) for call in actual]
    reference_keys = [_call_key(call) for call in reference]
    if mode == "strict":
        return float(actual_keys == reference_keys)
    if mode == "unordered":
        return float(Counter(actual_keys) == Counter(reference_keys))
    if mode == "subset":
        matched = sum((Counter(actual_keys) & Counter(reference_keys)).values())
        return matched / len(reference_keys)
    if mode == "superset":
        return float(not (Counter(reference_keys) - Counter(actual_keys)))
    raise ValueError(f"unknown trajectory match mode: {mode}")


MISSING = object()


def read_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return MISSING
        current = current[segment]
    return current


def check_assertion(state: dict[str, Any], assertion: AssertionSpec) -> bool:
    actual = read_path(state, assertion.path)
    if assertion.op == "eq":
        return actual is not MISSING and actual == assertion.expected
    if assertion.op == "contains":
        if actual is MISSING:
            return False
        try:
            return assertion.expected in actual
        except TypeError:
            return False
    if assertion.op == "exists":
        return actual is not MISSING
    if assertion.op == "not_exists":
        return actual is MISSING
    return False


def compute_cost(
    events: Iterable[TraceEvent],
    input_per_million: float = 14.0,
    output_per_million: float = 56.0,
    cached_per_million: float = 3.5,
    tool_cost: float = 0.01,
    *,
    model_parameters: dict[str, Any] | None = None,
) -> tuple[float | None, bool]:
    usage_events = [event for event in events if event.type.value == "usage"]
    tool_calls = sum(event.type.value == "tool.call" for event in events)
    if not usage_events:
        return None, False
    if model_parameters is not None:
        prices = candidate_pricing(model_parameters)
        if prices is None:
            return None, False
        input_per_million = prices["input_per_million"]
        output_per_million = prices["output_per_million"]
        cached_per_million = prices["cached_per_million"]
        tool_cost = prices["tool_cost"]
    usage = [Usage.model_validate(event.payload) for event in usage_events]
    input_tokens = sum(item.input_tokens for item in usage)
    output_tokens = sum(item.output_tokens for item in usage)
    cached_tokens = sum(item.cached_tokens for item in usage)
    cost = compute_usage_cost_cny(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        tool_calls=tool_calls,
        input_per_million=input_per_million,
        output_per_million=output_per_million,
        cached_per_million=cached_per_million,
        tool_cost=tool_cost,
    )
    return round(cost, 4), True


FAILURE_LABELS = {
    "tool_misuse": "工具误用",
    "context_loss": "上下文丢失",
    "loop": "循环调用",
    "unverified": "未验证结果",
    "premature_completion": "过早完成",
    "policy_violation": "策略违规",
    "timeout": "超时",
    "budget_exhausted": "预算耗尽",
    "environment_error": "环境错误",
    "protocol_error": "协议错误",
    "judge_disagreement": "裁判分歧",
    "unknown": "未知失败",
}


def attribute_failures(
    events: list[TraceEvent], allowed_tools: list[str], success: bool
) -> list[FailureEvidence]:
    evidence: list[FailureEvidence] = []
    calls = [event for event in events if event.type.value == "tool.call"]
    signatures = [
        (
            str(event.payload.get("name", "")),
            canonical_hash(event.payload.get("arguments", {})),
        )
        for event in calls
    ]

    for event in calls:
        if (
            event.payload.get("name") not in allowed_tools
            or event.payload.get("valid_args") is False
        ):
            evidence.append(
                FailureEvidence(
                    category="tool_misuse",
                    label=FAILURE_LABELS["tool_misuse"],
                    severity="high",
                    event_range=(event.seq, event.seq),
                    rule="tool.allowlist_or_schema",
                    explanation="调用了未授权工具或工具参数未通过 schema 校验。",
                )
            )
            break
    for index in range(max(0, len(signatures) - 3)):
        window = signatures[index : index + 4]
        if len(set(window)) == 1:
            start, end = calls[index].seq, calls[index + 3].seq
            evidence.append(
                FailureEvidence(
                    category="loop",
                    label=FAILURE_LABELS["loop"],
                    severity="high",
                    event_range=(start, end),
                    rule="tool.same_call_four_times",
                    explanation=f"工具 {window[0][0]} 以相同参数连续调用 4 次。",
                )
            )
            break
    final_events = [event for event in events if event.type.value == "final"]
    if final_events and final_events[-1].payload.get("verified") is False:
        event = final_events[-1]
        evidence.append(
            FailureEvidence(
                category="unverified",
                label=FAILURE_LABELS["unverified"],
                severity="medium",
                event_range=(event.seq, event.seq),
                rule="final.requires_verification",
                explanation="Agent 在没有验证工具结果的情况下生成最终答案。",
            )
        )
    if not success and final_events and final_events[-1].payload.get("declared_complete", True):
        event = final_events[-1]
        evidence.append(
            FailureEvidence(
                category="premature_completion",
                label=FAILURE_LABELS["premature_completion"],
                severity="high",
                event_range=(event.seq, event.seq),
                rule="final.failed_assertions",
                explanation="任务断言尚未满足，但 Agent 已宣布完成。",
            )
        )
    for event in events:
        if event.type.value == "error":
            category = str(event.payload.get("category", "environment_error"))
            category = category if category in FAILURE_LABELS else "environment_error"
            evidence.append(
                FailureEvidence(
                    category=category,
                    label=FAILURE_LABELS[category],
                    severity="critical",
                    event_range=(event.seq, event.seq),
                    rule="runtime.error_event",
                    explanation=str(event.payload.get("message", "运行环境返回错误。")),
                )
            )
    if not success and not evidence:
        last = events[-1].seq if events else 0
        evidence.append(
            FailureEvidence(
                category="unknown",
                label=FAILURE_LABELS["unknown"],
                severity="medium",
                event_range=(0, last),
                rule="fallback.unclassified",
                explanation="结果未通过断言，但确定性规则未识别出唯一原因。",
                confidence="low",
            )
        )
    severity = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(evidence, key=lambda item: (item.event_range[0], severity[item.severity]))


def aggregate_runs(runs: list[RunResult], bootstrap_samples: int = 1_500) -> dict[str, Any]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for run in runs:
        grouped[run.task_id].append(run.success)
    interval = stratified_bootstrap_interval(grouped, samples=bootstrap_samples)
    complete_costs = [
        run.cost_cny for run in runs if run.cost_complete and run.cost_cny is not None
    ]
    task_success_rates = [sum(values) / len(values) for values in grouped.values()]
    return {
        "success_rate": float(np.mean(task_success_rates)) if task_success_rates else 0.0,
        "success_interval": interval,
        "average_cost_cny": round(float(np.mean(complete_costs)), 3) if complete_costs else None,
        "cost_complete": bool(runs) and len(complete_costs) == len(runs),
        "average_tool_calls": round(float(np.mean([run.tool_calls for run in runs])), 1)
        if runs
        else 0.0,
        "average_trajectory_score": round(float(np.mean([run.trajectory_score for run in runs])), 3)
        if runs
        else 0.0,
        "by_task": {
            task_id: {
                "success_rate": sum(values) / len(values),
                "interval": wilson_interval(sum(values), len(values)),
                "runs": len(values),
            }
            for task_id, values in grouped.items()
        },
    }
