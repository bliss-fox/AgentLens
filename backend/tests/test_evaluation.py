import math

import pytest

from agentlens.demo import TASKS, build_demo_experiment, run_scripted_trial
from agentlens.evaluation import (
    check_assertion,
    compute_cost,
    paired_bootstrap_difference,
    trajectory_match,
    wilson_interval,
)
from agentlens.schemas import AssertionSpec, EventType, ToolCall, TraceEvent, Usage


def calls(*names):
    return [ToolCall(name=name, arguments={"id": 1}) for name in names]


def test_trajectory_match_modes():
    reference = calls("lookup", "verify")
    assert trajectory_match(reference, reference, "strict") == 1
    assert trajectory_match(calls("verify", "lookup"), reference, "unordered") == 1
    assert trajectory_match(calls("lookup"), reference, "subset") == 0.5
    assert trajectory_match(calls("lookup", "verify", "extra"), reference, "superset") == 1


def test_wilson_interval_is_bounded_and_not_point_estimate():
    low, high = wilson_interval(8, 10)
    assert 0 <= low < 0.8 < high <= 1


def test_paired_bootstrap_detects_better_candidate():
    candidate = {"a": [True] * 9 + [False], "b": [True] * 8 + [False] * 2}
    baseline = {"a": [True] * 5 + [False] * 5, "b": [True] * 4 + [False] * 6}
    point, low, high = paired_bootstrap_difference(candidate, baseline, samples=800)
    assert math.isclose(point, 0.4)
    assert high > low


def test_paired_bootstrap_aligns_by_shared_seed():
    candidate = {"a": {1: True, 2: False}}
    baseline = {"a": {2: True, 3: False}}
    point, low, high = paired_bootstrap_difference(candidate, baseline, samples=100)
    assert point == -1.0
    assert (low, high) == (-1.0, -1.0)


def test_assertion_existence_distinguishes_null_from_missing():
    assert check_assertion({"value": None}, AssertionSpec(path="value", op="exists"))
    assert not check_assertion({"value": None}, AssertionSpec(path="value", op="not_exists"))
    assert check_assertion({}, AssertionSpec(path="value", op="not_exists"))


def test_usage_rejects_cached_tokens_exceeding_input_tokens():
    with pytest.raises(ValueError, match="cached_tokens cannot exceed input_tokens"):
        Usage(input_tokens=10, cached_tokens=11)


def test_compute_cost_does_not_charge_cached_input_twice():
    events = [
        TraceEvent(
            run_id="run-cost",
            seq=0,
            type=EventType.USAGE,
            payload={"input_tokens": 1_000, "output_tokens": 100, "cached_tokens": 200},
        )
    ]

    cost, complete = compute_cost(events)

    assert complete is True
    assert cost == 0.0175


def test_compute_cost_uses_frozen_candidate_pricing():
    events = [
        TraceEvent(
            run_id="run-deepseek-cost",
            seq=0,
            type=EventType.USAGE,
            payload={"input_tokens": 1_000, "output_tokens": 100, "cached_tokens": 200},
        )
    ]
    parameters = {
        "pricing_cny": {
            "version": "test",
            "input_per_million": 2,
            "output_per_million": 8,
            "cached_per_million": 0.5,
            "tool_cost": 0,
        }
    }

    cost, complete = compute_cost(events, model_parameters=parameters)

    assert complete is True
    assert cost == 0.0025


def test_http_candidate_without_frozen_pricing_marks_cost_incomplete():
    events = [
        TraceEvent(
            run_id="run-unpriced",
            seq=0,
            type=EventType.USAGE,
            payload={"input_tokens": 100, "output_tokens": 20, "cached_tokens": 0},
        )
    ]

    assert compute_cost(events, model_parameters={}) == (None, False)


def test_scripted_run_is_reproducible_and_attributed():
    left = run_scripted_trial("support-v1.4", TASKS[0], 3)
    right = run_scripted_trial("support-v1.4", TASKS[0], 3)
    assert left.model_dump() == right.model_dump()
    assert left.failures[0].category == "loop"
    assert left.failures[0].judge_verdict == "not_run"


def test_demo_experiment_has_120_total_runs_and_calibrated_judge():
    experiment = build_demo_experiment("test", "test")
    assert experiment.completed_runs == experiment.total_runs == 120
    assert len(experiment.runs) == 60
    assert len(experiment.baseline_runs) == 60
    assert {run.candidate_id for run in experiment.baseline_runs} == {"support-v1.3"}
    assert experiment.judge_calibration["correct"] == 22
    assert experiment.judge_calibration["passed"] is True


def test_run_ids_are_unique_across_experiments():
    left = build_demo_experiment("experiment-a", "test")
    right = build_demo_experiment("experiment-b", "test")
    assert {run.run_id for run in left.runs}.isdisjoint(run.run_id for run in right.runs)
