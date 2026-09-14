import pytest

from agentlens.orchestrator import WORKFLOW_STEPS, run_evaluation_workflow


def test_langgraph_workflow_executes_all_audited_steps_in_order():
    calls = []
    handlers = {name: lambda name=name: calls.append(name) for name in WORKFLOW_STEPS}

    completed = run_evaluation_workflow(handlers)

    assert completed == WORKFLOW_STEPS
    assert calls == list(WORKFLOW_STEPS)


def test_langgraph_workflow_stops_at_the_failing_step():
    calls = []

    def handler(name):
        calls.append(name)
        if name == "run_trials":
            raise RuntimeError("trial failure")

    handlers = {name: lambda name=name: handler(name) for name in WORKFLOW_STEPS}

    with pytest.raises(RuntimeError, match="trial failure"):
        run_evaluation_workflow(handlers)

    assert calls == list(WORKFLOW_STEPS[:3])


def test_langgraph_workflow_rejects_incomplete_handler_sets():
    with pytest.raises(ValueError, match="missing=.*generate_report"):
        run_evaluation_workflow({name: lambda: None for name in WORKFLOW_STEPS[:-1]})
