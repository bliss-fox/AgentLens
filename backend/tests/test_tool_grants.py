import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from agentlens.database import (
    consume_tool_grant,
    engine,
    issue_tool_grant,
    maintain_tool_grants,
    revoke_tool_grant,
    session_factory,
    tool_grant_token_hash,
)
from agentlens.exceptions import ToolAuthorizationError
from agentlens.models import Base, ToolGrantRecord

CASSETTE_SHA256 = "a" * 64


@pytest.fixture(autouse=True)
def isolated_tool_grant_database():
    if str(engine().url) != "sqlite+pysqlite:///:memory:":
        raise RuntimeError("tests may only reset the isolated in-memory database")
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())


def issue(**overrides):
    values = {
        "run_id": "run-tool-grant",
        "experiment_id": "exp-tool-grant",
        "cassette_id": "customer-tools-v2",
        "cassette_content_sha256": CASSETTE_SHA256,
        "allowed_tools": ["search_customer"],
        "max_calls": 2,
        "ttl_seconds": 60,
        "now": datetime(2026, 8, 24, 8, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return issue_tool_grant(**values)


def consume(token, **overrides):
    values = {
        "token": token,
        "run_id": "run-tool-grant",
        "cassette_id": "customer-tools-v2",
        "cassette_content_sha256": CASSETTE_SHA256,
        "tool": "search_customer",
        "now": datetime(2026, 8, 24, 8, 0, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return consume_tool_grant(**values)


def test_tool_grant_stores_only_hash_and_consumes_budget():
    token = issue()
    token_hash = tool_grant_token_hash(token)

    with session_factory()() as session:
        record = session.get(ToolGrantRecord, token_hash)
        assert record is not None
        stored = {
            column.name: getattr(record, column.name)
            for column in ToolGrantRecord.__table__.columns
        }
        assert token not in json.dumps(stored, default=str)
        assert record.token_hash == token_hash != token
        assert record.allowed_tools == ["search_customer"]
        assert record.remaining_calls == 2
        assert record.status == "active"

    assert consume(token) == 1
    with session_factory()() as session:
        record = session.get(ToolGrantRecord, token_hash)
        assert record is not None
        assert record.remaining_calls == 1
        assert record.last_used_at is not None


@pytest.mark.parametrize(
    "override",
    [
        {"run_id": "other-run"},
        {"cassette_id": "other-cassette"},
        {"cassette_content_sha256": "b" * 64},
        {"tool": "get_order"},
    ],
)
def test_tool_grant_contract_mismatch_does_not_consume_budget(override):
    token = issue(max_calls=1)

    with pytest.raises(ToolAuthorizationError) as caught:
        consume(token, **override)

    assert caught.value.category == "tool_authorization_error"
    assert caught.value.status_code == 403
    with session_factory()() as session:
        record = session.get(ToolGrantRecord, tool_grant_token_hash(token))
        assert record is not None
        assert record.remaining_calls == 1


def test_expired_and_revoked_grants_are_durably_inactive():
    issued_at = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
    expired_token = issue(ttl_seconds=10, now=issued_at)

    with pytest.raises(ToolAuthorizationError) as expired:
        consume(expired_token, now=issued_at + timedelta(seconds=10))
    assert expired.value.status_code == 403
    with session_factory()() as session:
        record = session.get(ToolGrantRecord, tool_grant_token_hash(expired_token))
        assert record is not None
        assert record.status == "expired"

    active_token = issue(run_id="run-revoke")
    assert revoke_tool_grant(active_token, now=issued_at + timedelta(seconds=1))
    with pytest.raises(ToolAuthorizationError):
        consume(active_token, run_id="run-revoke")
    with session_factory()() as session:
        record = session.get(ToolGrantRecord, tool_grant_token_hash(active_token))
        assert record is not None
        assert record.status == "revoked"
        assert record.revoked_at is not None


def test_tool_grant_budget_is_consumed_atomically():
    token = issue(max_calls=1)

    def attempt():
        try:
            return ("allowed", consume(token))
        except ToolAuthorizationError as error:
            return (error.category, error.status_code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(2)))

    assert sorted(outcomes) == [("allowed", 0), ("tool_budget_exhausted", 429)]
    with session_factory()() as session:
        record = session.get(ToolGrantRecord, tool_grant_token_hash(token))
        assert record is not None
        assert record.remaining_calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": ""},
        {"experiment_id": ""},
        {"cassette_id": ""},
        {"cassette_content_sha256": "invalid"},
        {"max_calls": -1},
        {"ttl_seconds": 0},
        {"allowed_tools": [""]},
        {"allowed_tools": ["search_customer", 1]},
        {"ttl_seconds": True},
        {"run_id": 1},
    ],
)
def test_tool_grant_issuance_rejects_invalid_contract(overrides):
    with pytest.raises(ValueError):
        issue(**overrides)


def test_tool_grant_maintenance_retains_active_and_recent_audit_records():
    maintained_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    active = issue(run_id="run-active", now=maintained_at, ttl_seconds=60)
    recent_expired = issue(
        run_id="run-recent-expired",
        now=maintained_at - timedelta(seconds=10),
        ttl_seconds=10,
    )
    recent_revoked = issue(
        run_id="run-recent-revoked",
        now=maintained_at - timedelta(seconds=10),
        ttl_seconds=60,
    )
    assert revoke_tool_grant(
        recent_revoked,
        now=maintained_at - timedelta(seconds=1),
    )
    old_expired = issue(
        run_id="run-old-expired",
        now=maintained_at - timedelta(seconds=120),
        ttl_seconds=10,
    )
    old_revoked = issue(
        run_id="run-old-revoked",
        now=maintained_at - timedelta(seconds=120),
        ttl_seconds=300,
    )
    assert revoke_tool_grant(
        old_revoked,
        now=maintained_at - timedelta(seconds=60),
    )

    report = maintain_tool_grants(
        retention_seconds=60,
        batch_size=100,
        now=maintained_at,
    )

    assert report == {
        "active": 1,
        "expired": 1,
        "revoked": 1,
        "other": 0,
        "total": 3,
        "expired_marked": 2,
        "purged": 2,
        "retention_seconds": 60,
        "cleanup_batch_size": 100,
    }
    with session_factory()() as session:
        assert session.get(ToolGrantRecord, tool_grant_token_hash(active)).status == "active"
        assert (
            session.get(ToolGrantRecord, tool_grant_token_hash(recent_expired)).status
            == "expired"
        )
        assert (
            session.get(ToolGrantRecord, tool_grant_token_hash(recent_revoked)).status
            == "revoked"
        )
        assert session.get(ToolGrantRecord, tool_grant_token_hash(old_expired)) is None
        assert session.get(ToolGrantRecord, tool_grant_token_hash(old_revoked)) is None


def test_tool_grant_maintenance_is_bounded_per_batch():
    maintained_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    tokens = [
        issue(
            run_id=f"run-batch-{index}",
            now=maintained_at - timedelta(seconds=120 + index),
            ttl_seconds=1,
        )
        for index in range(3)
    ]

    report = maintain_tool_grants(
        retention_seconds=0,
        batch_size=1,
        now=maintained_at,
    )

    assert report["expired_marked"] == 1
    assert report["purged"] == 1
    assert report["active"] == 2
    with session_factory()() as session:
        assert sum(
            session.get(ToolGrantRecord, tool_grant_token_hash(token)) is not None
            for token in tokens
        ) == 2


def test_tool_grant_maintenance_does_not_race_active_consumption():
    issued_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    token = issue(
        run_id="run-concurrent-maintenance",
        max_calls=1,
        ttl_seconds=60,
        now=issued_at,
    )
    current_time = issued_at + timedelta(seconds=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        consumed = executor.submit(
            consume,
            token,
            run_id="run-concurrent-maintenance",
            now=current_time,
        )
        maintained = executor.submit(
            maintain_tool_grants,
            retention_seconds=60,
            batch_size=10,
            now=current_time,
        )

    assert consumed.result() == 0
    assert maintained.result()["purged"] == 0
    with session_factory()() as session:
        record = session.get(ToolGrantRecord, tool_grant_token_hash(token))
        assert record is not None
        assert record.status == "active"
        assert record.remaining_calls == 0


def test_tool_grant_retention_boundary_treats_naive_time_as_utc():
    maintained_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    token = issue(
        run_id="run-utc-boundary",
        now=maintained_at - timedelta(seconds=120),
        ttl_seconds=300,
    )
    assert revoke_tool_grant(
        token,
        now=maintained_at - timedelta(seconds=60),
    )

    report = maintain_tool_grants(
        retention_seconds=60,
        batch_size=10,
        now=maintained_at.replace(tzinfo=None),
    )

    assert report["purged"] == 1
    with session_factory()() as session:
        assert session.get(ToolGrantRecord, tool_grant_token_hash(token)) is None


@pytest.mark.parametrize(
    ("retention_seconds", "batch_size"),
    [
        (-1, 1),
        (1.5, 1),
        (True, 1),
        (0, 0),
        (0, 10_001),
        (0, 1.5),
        (0, True),
    ],
)
def test_tool_grant_maintenance_rejects_invalid_limits(
    retention_seconds,
    batch_size,
):
    with pytest.raises(ValueError):
        maintain_tool_grants(
            retention_seconds=retention_seconds,
            batch_size=batch_size,
        )
