from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from itertools import pairwise
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

WORKFLOW_STEPS = (
    "snapshot_environment",
    "calibrate_judge",
    "run_trials",
    "analyze_trajectories",
    "attribute_failures",
    "compare_candidates",
    "generate_report",
)


class EvaluationWorkflowState(TypedDict):
    handlers: Mapping[str, Callable[[], None]]
    completed_steps: Annotated[list[str], operator.add]


def _workflow_node(name: str):
    def execute(state: EvaluationWorkflowState) -> dict[str, list[str]]:
        state["handlers"][name]()
        return {"completed_steps": [name]}

    execute.__name__ = name
    return execute


def _compile_workflow():
    builder = StateGraph(EvaluationWorkflowState)
    for name in WORKFLOW_STEPS:
        builder.add_node(name, _workflow_node(name))
    builder.add_edge(START, WORKFLOW_STEPS[0])
    for current, following in pairwise(WORKFLOW_STEPS):
        builder.add_edge(current, following)
    builder.add_edge(WORKFLOW_STEPS[-1], END)
    return builder.compile()


_EVALUATION_WORKFLOW = _compile_workflow()


def run_evaluation_workflow(handlers: Mapping[str, Callable[[], None]]) -> tuple[str, ...]:
    missing = [name for name in WORKFLOW_STEPS if name not in handlers]
    unexpected = [name for name in handlers if name not in WORKFLOW_STEPS]
    if missing or unexpected:
        raise ValueError(
            f"invalid evaluation workflow handlers: missing={missing}, unexpected={unexpected}"
        )
    result = _EVALUATION_WORKFLOW.invoke(
        {"handlers": handlers, "completed_steps": []}
    )
    completed = tuple(result["completed_steps"])
    if completed != WORKFLOW_STEPS:
        raise RuntimeError("evaluation workflow did not complete every audited step")
    return completed
