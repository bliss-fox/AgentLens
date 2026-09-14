import ast
from pathlib import Path

import agentlens.database as database_facade
from agentlens import (
    database_core,
    experiment_repository,
    review_repository,
    tool_grant_repository,
)


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_database_facade_preserves_repository_api_identity():
    assert database_facade.EXPECTED_SCHEMA_REVISION == (database_core.EXPECTED_SCHEMA_REVISION)
    assert database_facade.engine is database_core.engine
    assert database_facade.session_factory is database_core.session_factory
    assert database_facade.init_database is database_core.init_database
    assert database_facade.schema_revision is database_core.schema_revision
    assert database_facade.sessions is database_core.sessions
    assert database_facade._database_lock is database_core.database_lock
    assert database_facade._as_utc is database_core.as_utc

    experiment_names = (
        "claim_experiment",
        "compare_and_set_status",
        "latest_persisted_experiment",
        "list_persisted_experiments",
        "load_execution_state",
        "load_experiment",
        "mark_experiment_cancelled",
        "mark_experiment_failed",
        "mark_stale_experiment",
        "mark_stale_experiments",
        "persist_experiment",
        "persist_queued_experiment",
        "persisted_counts",
        "update_experiment_progress",
        "update_experiment_status",
    )
    review_names = (
        "FailureReviewClaim",
        "claim_failure_review",
        "persist_failure_review",
        "release_failure_review_claim",
    )
    for name in experiment_names:
        assert getattr(database_facade, name) is getattr(experiment_repository, name)
    for name in review_names:
        assert getattr(database_facade, name) is getattr(review_repository, name)

    assert database_facade.issue_tool_grant is tool_grant_repository.issue_tool_grant
    assert database_facade.consume_tool_grant is tool_grant_repository.consume_tool_grant
    assert database_facade.revoke_tool_grant is tool_grant_repository.revoke_tool_grant
    assert database_facade.maintain_tool_grants is tool_grant_repository.maintain_tool_grants
    assert database_facade.tool_grant_token_hash is tool_grant_repository.tool_grant_token_hash


def test_database_facade_contains_no_implementation_definitions():
    path = Path(__file__).parents[1] / "agentlens" / "database.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    assert not any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        for node in ast.walk(tree)
    )


def test_repositories_depend_on_core_without_importing_database_facade():
    package = Path(__file__).parents[1] / "agentlens"
    core_imports = imported_modules(package / "database_core.py")
    experiment_imports = imported_modules(package / "experiment_repository.py")
    review_imports = imported_modules(package / "review_repository.py")
    grant_imports = imported_modules(package / "tool_grant_repository.py")

    forbidden_core = {
        "agentlens.database",
        "agentlens.store",
        "agentlens.tool_gateway",
        "agentlens.execution_contract",
    }
    forbidden_repository = {
        "agentlens.database",
        "agentlens.store",
        "agentlens.api",
        "agentlens.tool_gateway",
    }
    assert not (core_imports & forbidden_core)
    assert not (experiment_imports & forbidden_repository)
    assert not (review_imports & forbidden_repository)
    assert not (grant_imports & forbidden_repository)
    assert "agentlens.database_core" in experiment_imports
    assert "agentlens.database_core" in review_imports
    assert "agentlens.database_core" in grant_imports
    assert "agentlens.experiment_repository" in review_imports


def test_production_consumers_import_repositories_directly():
    backend = Path(__file__).parents[1]
    experiment_targets = (
        backend / "agentlens" / "routers" / "experiments.py",
        backend / "agentlens" / "routers" / "health.py",
        backend / "agentlens" / "store.py",
        backend / "agentlens" / "worker.py",
        backend / "scripts" / "run_offline_benchmark.py",
    )
    review_targets = (backend / "agentlens" / "routers" / "reviews.py",)
    grant_targets = (
        backend / "agentlens" / "routers" / "health.py",
        backend / "agentlens" / "routers" / "tools.py",
        backend / "agentlens" / "runtime.py",
        backend / "scripts" / "verify_compose_stack.py",
    )

    for target in experiment_targets:
        assert "agentlens.experiment_repository" in imported_modules(target)
    for target in review_targets:
        assert "agentlens.review_repository" in imported_modules(target)
    for target in grant_targets:
        assert "agentlens.tool_grant_repository" in imported_modules(target)


def test_api_composition_defers_store_and_cassette_construction():
    package = Path(__file__).parents[1] / "agentlens"
    api_tree = ast.parse((package / "api.py").read_text(encoding="utf-8"))
    store_tree = ast.parse((package / "store.py").read_text(encoding="utf-8"))
    gateway_tree = ast.parse((package / "tool_gateway.py").read_text(encoding="utf-8"))
    worker_tree = ast.parse((package / "worker.py").read_text(encoding="utf-8"))

    function_names = {node.name for node in api_tree.body if isinstance(node, ast.FunctionDef)}
    assert "create_app" in function_names

    eager_api_calls = {
        node.value.func.id
        for node in api_tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }
    assert not (eager_api_calls & {"ExperimentStore", "CassetteRegistry"})

    store_gateway_imports = {
        alias.name
        for node in store_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "agentlens.tool_gateway"
        for alias in node.names
    }
    assert "registry" not in store_gateway_imports
    assert {"CassetteRegistry", "get_default_registry"} <= store_gateway_imports

    assert not any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "CassetteRegistry"
        for node in gateway_tree.body
    )

    eager_worker_calls = {
        node.value.func.id
        for node in worker_tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }
    assert not (eager_worker_calls & {"ExperimentStore", "CassetteRegistry"})
    assert not any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "worker_store" for target in node.targets
        )
        for node in worker_tree.body
    )


def test_api_composition_root_has_domain_router_boundaries():
    package = Path(__file__).parents[1] / "agentlens"
    api_path = package / "api.py"
    api_imports = imported_modules(api_path)
    api_source = api_path.read_text(encoding="utf-8")
    router_paths = {
        "health.py": ("/health",),
        "experiments.py": (
            "/api/v1/bootstrap",
            "/api/v1/experiments",
            "/api/v1/tasks",
            "/api/v1/calibration",
        ),
        "reviews.py": ("/failures/{failure_index}/review",),
        "tools.py": (
            "/api/v1/tools/invoke",
            "/api/v1/cassettes/{cassette_id}",
        ),
    }

    assert "agentlens.routers" in api_imports
    assert not (
        api_imports
        & {
            "agentlens.experiment_repository",
            "agentlens.review_repository",
            "agentlens.tool_grant_repository",
            "agentlens.demo",
            "agentlens.judge",
        }
    )
    assert "@router." not in api_source

    for filename, expected_paths in router_paths.items():
        router_path = package / "routers" / filename
        router_source = router_path.read_text(encoding="utf-8")
        router_imports = imported_modules(router_path)
        assert "agentlens.api" not in router_imports
        assert "router = APIRouter(" in router_source
        for expected_path in expected_paths:
            assert expected_path in router_source
