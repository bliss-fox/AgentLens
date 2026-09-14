from __future__ import annotations

from fastapi import FastAPI

from agentlens.store import ExperimentStore
from agentlens.tool_gateway import CassetteRegistry


def cassette_registry_for(application: FastAPI) -> CassetteRegistry:
    current = application.state.cassette_registry
    if current is not None:
        return current
    with application.state.dependency_lock:
        current = application.state.cassette_registry
        if current is None:
            current = CassetteRegistry()
            application.state.cassette_registry = current
    return current


def experiment_store_for(application: FastAPI) -> ExperimentStore:
    current = application.state.experiment_store
    if current is not None:
        return current
    with application.state.dependency_lock:
        current = application.state.experiment_store
        if current is None:
            current = ExperimentStore(
                seed_demo=application.state.seed_demo,
                cassette_registry=cassette_registry_for(application),
            )
            application.state.experiment_store = current
    return current
