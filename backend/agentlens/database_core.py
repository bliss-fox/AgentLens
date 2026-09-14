from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache
from threading import RLock

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agentlens.config import get_settings
from agentlens.models import Base

database_lock = RLock()
EXPECTED_SCHEMA_REVISION = "0005_evaluation_assets"


@lru_cache
def engine():
    url = get_settings().database_url
    options = {"check_same_thread": False} if url.startswith("sqlite") else {}
    pool_options = {"poolclass": StaticPool} if ":memory:" in url else {}
    return create_engine(url, pool_pre_ping=True, connect_args=options, **pool_options)


@lru_cache
def session_factory():
    return sessionmaker(engine(), expire_on_commit=False)


def init_database() -> None:
    with database_lock:
        Base.metadata.create_all(engine())


def sessions() -> Iterator[Session]:
    with session_factory()() as session:
        yield session


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def schema_revision() -> str | None:
    with database_lock, engine().connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return None
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
