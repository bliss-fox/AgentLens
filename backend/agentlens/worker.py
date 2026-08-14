from typing import ClassVar

from arq.connections import RedisSettings

from agentlens.config import get_settings
from agentlens.schemas import ExperimentRequest
from agentlens.store import store


async def execute_experiment(ctx, payload: dict) -> str:
    request = ExperimentRequest.model_validate(payload)
    return store.create(request).id


class WorkerSettings:
    functions: ClassVar = [execute_experiment]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
