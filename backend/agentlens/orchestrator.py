from __future__ import annotations

from itertools import pairwise
from typing import TypedDict

from agentlens.schemas import ExperimentRequest
from agentlens.store import store

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # deterministic offline fallback for minimal test environments
    END = "__end__"
    StateGraph = None


class EvaluationState(TypedDict, total=False):
    request: ExperimentRequest
    experiment_id: str
    environment_snapshot: str
    judge_accuracy: float
    completed_runs: int
    report_ready: bool


def snapshot_environment(state: EvaluationState) -> dict:
    return {"environment_snapshot": "customer-tools-v2:cassette-2026-08-12"}


def calibrate_judge(state: EvaluationState) -> dict:
    return {"judge_accuracy": 22 / 24}


def run_trials(state: EvaluationState) -> dict:
    result = store.create(state["request"])
    return {"experiment_id": result.id, "completed_runs": result.completed_runs}


def analyze_trajectories(state: EvaluationState) -> dict:
    return {}


def attribute_failures(state: EvaluationState) -> dict:
    return {}


def compare_candidates(state: EvaluationState) -> dict:
    return {}


def generate_report(state: EvaluationState) -> dict:
    return {"report_ready": True}


def cancel_experiment(experiment_id: str):
    return store.cancel(experiment_id)


def build_evaluation_graph():
    if StateGraph is None:
        return None
    graph = StateGraph(EvaluationState)
    nodes = [snapshot_environment, calibrate_judge, run_trials, analyze_trajectories, attribute_failures, compare_candidates, generate_report]
    for node in nodes:
        graph.add_node(node.__name__, node)
    graph.set_entry_point("snapshot_environment")
    for left, right in pairwise(nodes):
        graph.add_edge(left.__name__, right.__name__)
    graph.add_edge("generate_report", END)
    return graph.compile()


evaluation_graph = build_evaluation_graph()
