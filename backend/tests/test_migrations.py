import json
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from agentlens.config import get_settings
from agentlens.database import EXPECTED_SCHEMA_REVISION
from alembic import command


def test_alembic_upgrade_matches_orm_metadata(monkeypatch):
    backend_dir = Path(__file__).parents[1]
    database_path = backend_dir / f".migration-check-{uuid4().hex}.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    migration_engine = None

    try:
        assert ScriptDirectory.from_config(config).get_current_head() == EXPECTED_SCHEMA_REVISION
        command.upgrade(config, "0001_initial")
        migration_engine = create_engine(database_url)
        with migration_engine.begin() as connection:
            candidate_values = {
                "name": "Legacy Agent",
                "version": "v1",
                "fingerprint": json.dumps({"model": "legacy"}),
            }
            connection.execute(
                text(
                    "INSERT INTO candidates "
                    "(id, name, version, fingerprint, created_at) "
                    "VALUES (:id, :name, :version, :fingerprint, NULL)"
                ),
                [
                    {"id": "legacy-candidate", **candidate_values},
                    {"id": "legacy-baseline", **candidate_values},
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO experiments "
                    "(id, status, prompt, candidate_id, baseline_id, snapshot, metrics, created_at) "
                    "VALUES (:id, :status, :prompt, :candidate_id, :baseline_id, "
                    ":snapshot, NULL, NULL)"
                ),
                {
                    "id": "legacy-experiment",
                    "status": "queued",
                    "prompt": "legacy",
                    "candidate_id": "legacy-candidate",
                    "baseline_id": "legacy-baseline",
                    "snapshot": json.dumps({}),
                },
            )
        migration_engine.dispose()
        migration_engine = None

        command.upgrade(config, "head")
        migration_engine = create_engine(database_url)
        inspector = inspect(migration_engine)
        assert "alembic_version" in inspector.get_table_names()
        assert {
            column["name"]: column["nullable"] for column in inspector.get_columns("candidates")
        }["created_at"] is False
        assert {
            column["name"]: column["nullable"] for column in inspector.get_columns("experiments")
        }["created_at"] is False
        assert inspector.get_unique_constraints("events") == [
            {
                "name": "uq_run_event_seq",
                "column_names": ["run_id", "seq"],
            }
        ]
        event_columns = {
            column["name"]: column for column in inspector.get_columns("events")
        }
        assert event_columns["span_id"]["nullable"] is True
        assert event_columns["parent_span_id"]["nullable"] is True
        tool_grant_columns = {
            column["name"]: column
            for column in inspector.get_columns("tool_grants")
        }
        assert set(tool_grant_columns) == {
            "token_hash",
            "run_id",
            "experiment_id",
            "cassette_id",
            "cassette_content_sha256",
            "allowed_tools",
            "remaining_calls",
            "status",
            "expires_at",
            "created_at",
            "last_used_at",
            "revoked_at",
        }
        assert tool_grant_columns["token_hash"]["nullable"] is False
        assert tool_grant_columns["created_at"]["nullable"] is False
        assert tool_grant_columns["last_used_at"]["nullable"] is True
        assert {
            index["name"] for index in inspector.get_indexes("tool_grants")
        } == {
            "ix_tool_grants_run_id",
            "ix_tool_grants_experiment_id",
            "ix_tool_grants_status",
            "ix_tool_grants_expires_at",
        }
        with migration_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM candidates WHERE created_at IS NULL")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM experiments WHERE created_at IS NULL")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == EXPECTED_SCHEMA_REVISION
            )
        command.check(config)
    finally:
        if migration_engine is not None:
            migration_engine.dispose()
        get_settings.cache_clear()
        database_path.unlink(missing_ok=True)
