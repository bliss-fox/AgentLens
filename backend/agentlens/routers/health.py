from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status

from agentlens.config import (
    ARQ_WORKER_HEALTH_CHECK_KEY,
    EXPERIMENT_HEARTBEAT_TIMEOUT_SECONDS,
    get_settings,
)
from agentlens.database_core import EXPECTED_SCHEMA_REVISION, schema_revision
from agentlens.experiment_repository import mark_stale_experiments, persisted_counts
from agentlens.tool_grant_repository import maintain_tool_grants

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request, response: Response) -> dict:
    settings = get_settings()
    execution_backend = settings.execution_backend
    revision = None
    storage = None
    database_ready = True
    try:
        _, grant_maintenance = await asyncio.gather(
            asyncio.to_thread(
                mark_stale_experiments,
                EXPERIMENT_HEARTBEAT_TIMEOUT_SECONDS,
            ),
            asyncio.to_thread(
                maintain_tool_grants,
                retention_seconds=settings.tool_grant_retention_seconds,
                batch_size=settings.tool_grant_cleanup_batch_size,
            ),
        )
        revision, storage = await asyncio.gather(
            asyncio.to_thread(schema_revision),
            asyncio.to_thread(persisted_counts),
        )
        storage["tool_grants"] = grant_maintenance
    except Exception:  # noqa: BLE001 - readiness never exposes database connection details.
        database_ready = False
    schema_ready = database_ready and (
        execution_backend != "arq" or revision == EXPECTED_SCHEMA_REVISION
    )
    queue_ready = True
    worker_ready = True
    queue_status = "not_required"
    worker_status = "not_required"
    if execution_backend == "arq":
        pool = getattr(request.app.state, "redis_pool", None)
        try:
            queue_ready = pool is not None and bool(await pool.ping())
            worker_ready = queue_ready and bool(
                await pool.get(ARQ_WORKER_HEALTH_CHECK_KEY)
            )
        except Exception:  # noqa: BLE001 - readiness never exposes connection details.
            queue_ready = False
            worker_ready = False
        queue_status = "ok" if queue_ready else "unavailable"
        worker_status = "ok" if worker_ready else "unavailable"
    ready = database_ready and schema_ready and queue_ready and worker_ready
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if ready else "degraded",
        "mode": "controlled-evaluation",
        "execution_backend": execution_backend,
        "database_status": "ok" if database_ready else "unavailable",
        "schema_revision": revision,
        "queue_status": queue_status,
        "worker_status": worker_status,
        "storage": storage,
    }
