import pytest

from agentlens.asset_repository import (
    default_benchmark,
    ensure_default_assets,
    get_benchmark,
    get_candidate,
    save_benchmark,
    sync_candidate_identity,
)
from agentlens.database import engine
from agentlens.experiment_repository import load_execution_state
from agentlens.models import Base
from agentlens.schemas import ExperimentRequest
from agentlens.store import ExperimentStore


@pytest.fixture(autouse=True)
def isolated_asset_database():
    if str(engine().url) != "sqlite+pysqlite:///:memory:":
        raise RuntimeError("tests may only reset the isolated in-memory database")
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())


def test_default_real_assets_are_persisted_and_editable():
    ensure_default_assets()
    candidate = get_candidate("coding-assistant-v1")
    benchmark = get_benchmark("coding-agent-core-v1")

    assert candidate is not None
    assert candidate.endpoint == "http://candidate-agent:8100"
    assert candidate.model == "unverified-runtime-model"
    assert candidate.model_parameters["identity_status"] == "pending-readiness"
    assert benchmark is not None
    assert len(benchmark.tasks) == 6
    assert benchmark.environment_snapshot.startswith("coding-agent-v1:")

    updated = benchmark.model_copy(update={"name": "Updated coding benchmark"})
    save_benchmark(updated)
    assert get_benchmark(benchmark.id).name == "Updated coding benchmark"


def test_candidate_readiness_identity_replaces_placeholder_fingerprint():
    ensure_default_assets()
    candidate = get_candidate("coding-assistant-v1")
    assert candidate is not None
    synced = sync_candidate_identity(
        candidate,
        {
            "identity": {
                "schema": "agentlens.candidate-identity/v1",
                "model": "deepseek-chat",
                "model_parameters": {
                    "provider": "deepseek",
                    "tool_choice": "auto",
                    "seed_parameter": False,
                },
                "prompt_hash": "sha256:prompt-content",
                "scaffold_version": "agentlens-openai-compat@0.1.0",
                "tool_schema_hash": "sha256:tool-content",
            }
        },
    )

    assert synced.model == "deepseek-chat"
    assert synced.model_parameters["provider"] == "deepseek"
    assert get_candidate(candidate.id).prompt_hash == "sha256:prompt-content"


def test_single_candidate_work_total_and_snapshot_are_immutable():
    store = ExperimentStore(seed_demo=False)
    request = ExperimentRequest(
        prompt="real single candidate",
        candidate_id="coding-assistant-v1",
        baseline_candidate_id=None,
        benchmark_id="coding-agent-core-v1",
        execution_mode="http",
        repetitions=3,
    )
    queued = store.create_queued(request, experiment_id="exp-real-single")
    state_before = load_execution_state(queued.id)

    assert queued.baseline is None
    assert queued.total_runs == 18
    assert state_before["progress"] == {"completed": 0, "total": 18}
    assert state_before["snapshot"]["benchmark_id"] == "coding-agent-core-v1"
    assert len(state_before["snapshot"]["tasks"]) == 6

    benchmark = default_benchmark().model_copy(update={"name": "Changed later"})
    save_benchmark(benchmark)
    state_after = load_execution_state(queued.id)
    assert state_after["snapshot"] == state_before["snapshot"]


def test_ab_work_total_counts_both_candidates():
    ensure_default_assets()
    candidate = get_candidate("coding-assistant-v1")
    assert candidate is not None
    second = candidate.model_copy(
        update={"id": "coding-assistant-v2", "version": "v2"}
    )
    from agentlens.asset_repository import save_candidate

    save_candidate(second)
    queued = ExperimentStore(seed_demo=False).create_queued(
        ExperimentRequest(
            prompt="real paired candidates",
            candidate_id=candidate.id,
            baseline_candidate_id=second.id,
            benchmark_id="coding-agent-core-v1",
            execution_mode="http",
            repetitions=1,
        ),
        experiment_id="exp-real-paired",
    )

    assert queued.baseline is not None
    assert queued.total_runs == 12
    state = load_execution_state(queued.id)
    assert state["snapshot"]["baseline"]["id"] == "coding-assistant-v2"
