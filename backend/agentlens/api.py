from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from threading import RLock

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agentlens.application_dependencies import experiment_store_for
from agentlens.config import get_settings
from agentlens.routers import assets, experiments, health, reviews, tools
from agentlens.store import ExperimentStore
from agentlens.tool_gateway import CassetteRegistry


@asynccontextmanager
async def lifespan(application: FastAPI):
    pool = None
    experiment_store = None
    try:
        experiment_store = experiment_store_for(application)
        if get_settings().execution_backend == "arq":
            pool = await create_pool(
                RedisSettings.from_dsn(get_settings().redis_url)
            )
        application.state.redis_pool = pool
        yield
    finally:
        if experiment_store is not None:
            await asyncio.to_thread(experiment_store.shutdown)
        if pool is not None:
            await pool.close()


async def validation_error_response(
    _request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    public_errors = [
        {
            "type": item.get("type", "value_error"),
            "loc": list(item.get("loc", ())),
            "msg": item.get("msg", "request validation failed"),
        }
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": public_errors},
    )


def create_app(
    *,
    experiment_store: ExperimentStore | None = None,
    cassette_registry: CassetteRegistry | None = None,
    seed_demo: bool = False,
) -> FastAPI:
    application = FastAPI(
        title="AgentLens API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.dependency_lock = RLock()
    application.state.experiment_store = experiment_store
    application.state.cassette_registry = cassette_registry
    application.state.seed_demo = seed_demo
    application.state.redis_pool = None
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_exception_handler(
        RequestValidationError,
        validation_error_response,
    )
    application.include_router(health.router)
    application.include_router(assets.router)
    application.include_router(experiments.router)
    application.include_router(reviews.router)
    application.include_router(tools.router)
    return application


app = create_app()
