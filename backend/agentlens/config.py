from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentlens.cassette import parse_environment_snapshot
from agentlens.schemas import CandidateSpec, normalize_http_url

BENCHMARK_ID = "customer-tools-v2"
BENCHMARK_NAME = "客服工具任务集 / v2"
TASK_SET_VERSION = "customer-tools-v2@2026-08-12"
CALIBRATION_VERSION = "scripted-calibration@2026-08-12"
EVALUATOR_VERSION = "agentlens-evaluator@0.1.0"
PRICE_TABLE_VERSION = "pricing-cny-2026-08"

ARQ_QUEUE_NAME = "arq:queue"
ARQ_WORKER_HEALTH_CHECK_KEY = "agentlens:worker:health"
ARQ_WORKER_HEALTH_CHECK_INTERVAL_SECONDS = 10
# A single HTTP trial is budgeted for at most 180 seconds. Progress renews the
# experiment heartbeat after every candidate/baseline trial.
EXPERIMENT_HEARTBEAT_TIMEOUT_SECONDS = 300
# 50 repetitions * 6 tasks * candidate/baseline * 180 seconds + shutdown margin.
ARQ_EXPERIMENT_JOB_TIMEOUT_SECONDS = 108_600
WORKER_INTERRUPT_GRACE_SECONDS = 2.0
JUDGE_REVIEW_LEASE_GRACE_SECONDS = 30.0
TOOL_GRANT_EXPIRY_GRACE_SECONDS = 30.0
TOOL_GRANT_RETENTION_SECONDS = 7 * 24 * 60 * 60
TOOL_GRANT_CLEANUP_BATCH_SIZE = 1_000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Local/offline mode uses an in-process shared SQLite database. Docker overrides
    # this with PostgreSQL, which is the durable deployment store.
    database_url: str = "sqlite+pysqlite:///:memory:"
    redis_url: str = "redis://localhost:6379/0"
    execution_backend: Literal["local", "arq"] = "local"
    agent_endpoints: dict[str, str] = Field(default_factory=dict)
    candidate_overrides: dict[str, CandidateSpec] = Field(default_factory=dict)
    environment_snapshot: str = "customer-tools-v2:cassette-2026-08-12"
    tool_gateway_url: str = "http://127.0.0.1:8000/api/v1/tools/invoke"
    tool_runner_url: str = "http://127.0.0.1:8200"
    tool_runner_timeout_seconds: float = Field(default=25.0, ge=1.0, le=120.0)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str | None = None
    judge_provider: Literal["openai", "deepseek"] = "openai"
    judge_model: str = Field(default="gpt-4.1-mini", min_length=1, max_length=200)
    judge_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    judge_max_context_bytes: int = Field(
        default=131_072,
        ge=1_024,
        le=1_048_576,
    )
    tool_grant_retention_seconds: int = Field(
        default=TOOL_GRANT_RETENTION_SECONDS,
        ge=3_600,
        le=31_536_000,
    )
    tool_grant_cleanup_batch_size: int = Field(
        default=TOOL_GRANT_CLEANUP_BATCH_SIZE,
        ge=1,
        le=10_000,
    )
    bootstrap_samples: int = 1_500

    @field_validator("tool_gateway_url")
    @classmethod
    def validate_tool_gateway_url(cls, value: str) -> str:
        return normalize_http_url(value, "TOOL_GATEWAY_URL")

    @field_validator("tool_runner_url")
    @classmethod
    def validate_tool_runner_url(cls, value: str) -> str:
        return normalize_http_url(value, "TOOL_RUNNER_URL")

    @field_validator("environment_snapshot")
    @classmethod
    def validate_environment_snapshot(cls, value: str) -> str:
        parse_environment_snapshot(value)
        return value

    @field_validator("openai_base_url", "deepseek_base_url")
    @classmethod
    def validate_openai_base_url(cls, value: str) -> str:
        return normalize_http_url(value, "JUDGE_BASE_URL")

    @property
    def judge_api_key(self) -> str | None:
        value = (
            self.deepseek_api_key
            if self.judge_provider == "deepseek"
            else self.openai_api_key
        )
        return value.strip() if value and value.strip() else None

    @property
    def judge_base_url(self) -> str:
        return (
            self.deepseek_base_url
            if self.judge_provider == "deepseek"
            else self.openai_base_url
        )

    @field_validator("judge_model")
    @classmethod
    def validate_judge_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("JUDGE_MODEL must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def candidate_override_ids_match_keys(self):
        mismatches = [
            key for key, candidate in self.candidate_overrides.items() if key != candidate.id
        ]
        if mismatches:
            raise ValueError(
                "CANDIDATE_OVERRIDES keys must match each nested candidate id: "
                + ", ".join(sorted(mismatches))
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
