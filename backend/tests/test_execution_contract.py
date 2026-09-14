import ast
from copy import deepcopy
from pathlib import Path

import pytest

from agentlens.config import get_settings
from agentlens.demo import TASKS, build_pending_experiment
from agentlens.execution_contract import (
    build_experiment_snapshot,
    validate_execution_contract,
)
from agentlens.schemas import ExperimentRequest
from agentlens.tool_gateway import registry


def queued_state():
    request = ExperimentRequest(prompt="contract boundary", repetitions=1)
    summary = build_pending_experiment(
        "exp-contract-boundary",
        request.prompt,
        request.candidate_id,
        request.baseline_candidate_id,
        request.repetitions,
        request.execution_mode,
        task_snapshot=TASKS,
    )
    settings = get_settings()
    cassette_contract = registry.environment_contract(settings.environment_snapshot)
    snapshot = build_experiment_snapshot(
        summary,
        TASKS,
        environment_snapshot=settings.environment_snapshot,
        cassette_contract=cassette_contract,
        request=request,
        tool_gateway_url=settings.tool_gateway_url,
    )
    return {
        "request": request.model_dump(mode="json"),
        "snapshot": snapshot,
        "progress": {"completed": 0, "total": 12},
        "prompt": request.prompt,
        "experiment": summary,
    }, cassette_contract


def test_contract_builder_freezes_adapter_data_and_validator_returns_typed_values():
    state, adapter_contract = queued_state()
    adapter_contract["cassette"]["content_sha256"] = "0" * 64

    contract = validate_execution_contract(
        state,
        registry.environment_contract,
    )

    assert contract.request.prompt == "contract boundary"
    assert contract.candidate.id == "support-v1.4"
    assert contract.baseline.id == "support-v1.3"
    assert len(contract.tasks) == 6
    assert contract.cassette_content_sha256 != "0" * 64
    assert contract.tool_gateway_url == get_settings().tool_gateway_url


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state["progress"].update(total=10),
        lambda state: state["snapshot"].update(price_table="future"),
        lambda state: state["snapshot"].update(execution_mode="http"),
    ],
)
def test_contract_validator_rejects_cross_layer_drift(mutate):
    state, _ = queued_state()
    mutate(state)

    with pytest.raises((TypeError, ValueError)):
        validate_execution_contract(state, registry.environment_contract)


def test_contract_validator_rejects_adapter_content_drift():
    state, _ = queued_state()
    drifted = deepcopy(state["snapshot"]["cassette_contract"])
    drifted["cassette"]["content_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="drifted"):
        validate_execution_contract(state, lambda _snapshot: drifted)


def test_queued_snapshot_requires_an_explicit_gateway_boundary():
    state, _ = queued_state()

    with pytest.raises(ValueError, match="tool gateway"):
        build_experiment_snapshot(
            state["experiment"],
            TASKS,
            environment_snapshot=state["snapshot"]["cassette"],
            cassette_contract=state["snapshot"]["cassette_contract"],
            request=ExperimentRequest(prompt="missing gateway", repetitions=1),
        )


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }


def test_persistence_layer_has_no_tool_gateway_or_contract_composition_dependency():
    package = Path(__file__).parents[1] / "agentlens"
    database_imports = imported_modules(package / "database.py")
    contract_imports = imported_modules(package / "execution_contract.py")

    assert "agentlens.tool_gateway" not in database_imports
    assert "agentlens.execution_contract" not in database_imports
    assert "agentlens.database" not in contract_imports
    assert "agentlens.store" not in contract_imports
