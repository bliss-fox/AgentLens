import math

from agentlens.demo import TASKS, build_demo_experiment, run_scripted_trial
from agentlens.evaluation import paired_bootstrap_difference, trajectory_match, wilson_interval
from agentlens.schemas import ToolCall


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


def test_scripted_run_is_reproducible_and_attributed():
    left = run_scripted_trial("support-v1.4", TASKS[0], 3)
    right = run_scripted_trial("support-v1.4", TASKS[0], 3)
    assert left.model_dump() == right.model_dump()
    assert left.failures[0].category == "loop"


def test_demo_experiment_has_sixty_runs_and_calibrated_judge():
    experiment = build_demo_experiment("test", "test")
    assert experiment.total_runs == 60
    assert len(experiment.runs) == 60
    assert experiment.judge_calibration["correct"] == 22
    assert experiment.judge_calibration["passed"] is True


def test_run_ids_are_unique_across_experiments():
    left = build_demo_experiment("experiment-a", "test")
    right = build_demo_experiment("experiment-b", "test")
    assert {run.run_id for run in left.runs}.isdisjoint(run.run_id for run in right.runs)
