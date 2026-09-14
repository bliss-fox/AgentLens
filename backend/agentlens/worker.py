import asyncio
from contextlib import suppress
from typing import ClassVar

from arq.connections import RedisSettings

from agentlens.config import (
    ARQ_EXPERIMENT_JOB_TIMEOUT_SECONDS,
    ARQ_QUEUE_NAME,
    ARQ_WORKER_HEALTH_CHECK_INTERVAL_SECONDS,
    ARQ_WORKER_HEALTH_CHECK_KEY,
    WORKER_INTERRUPT_GRACE_SECONDS,
    get_settings,
)
from agentlens.experiment_repository import mark_experiment_failed
from agentlens.store import ExperimentStore
from agentlens.tool_gateway import CassetteRegistry

_WORKER_STORE_KEY = "experiment_store"
_WORKER_REGISTRY_KEY = "cassette_registry"


def _worker_store_for(ctx: dict) -> ExperimentStore:
    experiment_store = ctx.get(_WORKER_STORE_KEY)
    if experiment_store is not None:
        return experiment_store
    cassette_registry = ctx.get(_WORKER_REGISTRY_KEY)
    if cassette_registry is None:
        cassette_registry = CassetteRegistry()
        ctx[_WORKER_REGISTRY_KEY] = cassette_registry
    experiment_store = ExperimentStore(cassette_registry=cassette_registry)
    ctx[_WORKER_STORE_KEY] = experiment_store
    return experiment_store


async def startup(ctx: dict) -> None:
    _worker_store_for(ctx)


async def shutdown(ctx: dict) -> None:
    experiment_store = ctx.get(_WORKER_STORE_KEY)
    if experiment_store is not None:
        await asyncio.to_thread(experiment_store.shutdown)


async def execute_experiment(
    ctx: dict,
    experiment_id: str,
    _legacy_payload: dict | None = None,
) -> str:
    """Execute the persisted request; legacy payloads are accepted but never trusted."""
    experiment_store = _worker_store_for(ctx)
    execution_task = asyncio.create_task(
        asyncio.to_thread(experiment_store.execute, experiment_id)
    )
    try:
        result = await asyncio.shield(execution_task)
    except asyncio.CancelledError:
        await asyncio.shield(
            asyncio.to_thread(
                mark_experiment_failed,
                experiment_id,
                "worker_interrupted",
                "experiment worker was interrupted",
            )
        )
        # Cleanup failure must not replace the authoritative ARQ cancellation.
        with suppress(Exception):
            await asyncio.wait_for(
                asyncio.shield(execution_task),
                timeout=WORKER_INTERRUPT_GRACE_SECONDS,
            )
        raise
    if result is None:
        raise LookupError(f"experiment {experiment_id} does not exist")
    return experiment_id


class WorkerSettings:
    functions: ClassVar = [execute_experiment]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    queue_name = ARQ_QUEUE_NAME
    health_check_key = ARQ_WORKER_HEALTH_CHECK_KEY
    health_check_interval = ARQ_WORKER_HEALTH_CHECK_INTERVAL_SECONDS
    job_timeout = ARQ_EXPERIMENT_JOB_TIMEOUT_SECONDS
