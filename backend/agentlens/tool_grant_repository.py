from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select

from agentlens.database_core import (
    as_utc,
    database_lock,
    init_database,
    session_factory,
)
from agentlens.exceptions import ToolAuthorizationError
from agentlens.models import ToolGrantRecord


def tool_grant_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_tool_grant(
    *,
    run_id: str,
    experiment_id: str,
    cassette_id: str,
    cassette_content_sha256: str,
    allowed_tools: Iterable[str],
    max_calls: int,
    ttl_seconds: float,
    now: datetime | None = None,
) -> str:
    if not isinstance(run_id, str) or not run_id or len(run_id) > 120:
        raise ValueError("tool grant run_id is invalid")
    if (
        not isinstance(experiment_id, str)
        or not experiment_id
        or len(experiment_id) > 80
    ):
        raise ValueError("tool grant experiment_id is invalid")
    if not isinstance(cassette_id, str) or not cassette_id or len(cassette_id) > 80:
        raise ValueError("tool grant cassette_id is invalid")
    if (
        not isinstance(cassette_content_sha256, str)
        or len(cassette_content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in cassette_content_sha256)
    ):
        raise ValueError("tool grant cassette digest is invalid")
    if type(max_calls) is not int or max_calls < 0:
        raise ValueError("tool grant max_calls must be a non-negative integer")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int | float)
        or ttl_seconds <= 0
    ):
        raise ValueError("tool grant TTL must be positive")
    raw_tools = tuple(allowed_tools)
    if any(not isinstance(tool, str) or not tool or len(tool) > 200 for tool in raw_tools):
        raise ValueError("tool grant allowlist is invalid")
    frozen_tools = sorted(set(raw_tools))
    issued_at = as_utc(now or datetime.now(UTC))
    token = secrets.token_urlsafe(32)
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        session.add(
            ToolGrantRecord(
                token_hash=tool_grant_token_hash(token),
                run_id=run_id,
                experiment_id=experiment_id,
                cassette_id=cassette_id,
                cassette_content_sha256=cassette_content_sha256,
                allowed_tools=frozen_tools,
                remaining_calls=max_calls,
                status="active",
                expires_at=issued_at + timedelta(seconds=ttl_seconds),
                created_at=issued_at,
            )
        )
    return token


def consume_tool_grant(
    *,
    token: str,
    run_id: str,
    cassette_id: str,
    cassette_content_sha256: str,
    tool: str,
    now: datetime | None = None,
) -> int:
    token_hash = tool_grant_token_hash(token)
    consumed_at = as_utc(now or datetime.now(UTC))
    authorization_error: ToolAuthorizationError | None = None
    remaining_calls: int | None = None
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ToolGrantRecord, token_hash, with_for_update=True)
        if record is None or record.status != "active":
            authorization_error = ToolAuthorizationError(
                "tool_authorization_error",
                "tool grant is invalid or inactive",
                status_code=403,
            )
        elif as_utc(record.expires_at) <= consumed_at:
            record.status = "expired"
            authorization_error = ToolAuthorizationError(
                "tool_authorization_error",
                "tool grant is invalid or inactive",
                status_code=403,
            )
        elif not (
            record.run_id == run_id
            and record.cassette_id == cassette_id
            and hmac.compare_digest(
                record.cassette_content_sha256,
                cassette_content_sha256,
            )
            and tool in record.allowed_tools
        ):
            authorization_error = ToolAuthorizationError(
                "tool_authorization_error",
                "tool grant does not authorize this invocation",
                status_code=403,
            )
        elif record.remaining_calls <= 0:
            authorization_error = ToolAuthorizationError(
                "tool_budget_exhausted",
                "tool grant call budget is exhausted",
                status_code=429,
            )
        else:
            record.remaining_calls -= 1
            record.last_used_at = consumed_at
            remaining_calls = record.remaining_calls
    if authorization_error is not None:
        raise authorization_error
    if remaining_calls is None:
        raise RuntimeError("tool grant transition produced no result")
    return remaining_calls

def revoke_tool_grant(token: str, now: datetime | None = None) -> bool:
    token_hash = tool_grant_token_hash(token)
    revoked_at = as_utc(now or datetime.now(UTC))
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        record = session.get(ToolGrantRecord, token_hash, with_for_update=True)
        if record is None:
            return False
        if record.status == "active":
            record.status = "revoked"
            record.revoked_at = revoked_at
        return True


def maintain_tool_grants(
    *,
    retention_seconds: int,
    batch_size: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Expire due grants and purge only terminal records past retention."""
    if type(retention_seconds) is not int or retention_seconds < 0:
        raise ValueError("tool grant retention must be a non-negative integer")
    if type(batch_size) is not int or batch_size <= 0 or batch_size > 10_000:
        raise ValueError("tool grant cleanup batch size must be between 1 and 10000")
    maintained_at = as_utc(now or datetime.now(UTC))
    cutoff = maintained_at - timedelta(seconds=retention_seconds)
    init_database()
    with database_lock, session_factory()() as session, session.begin():
        due = session.scalars(
            select(ToolGrantRecord)
            .where(
                ToolGrantRecord.status == "active",
                ToolGrantRecord.expires_at <= maintained_at,
            )
            .order_by(ToolGrantRecord.expires_at, ToolGrantRecord.token_hash)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        for record in due:
            record.status = "expired"
        session.flush()

        purgeable = session.scalars(
            select(ToolGrantRecord)
            .where(
                or_(
                    and_(
                        ToolGrantRecord.status == "expired",
                        ToolGrantRecord.expires_at <= cutoff,
                    ),
                    and_(
                        ToolGrantRecord.status == "revoked",
                        ToolGrantRecord.revoked_at.is_not(None),
                        ToolGrantRecord.revoked_at <= cutoff,
                    ),
                )
            )
            .order_by(ToolGrantRecord.expires_at, ToolGrantRecord.token_hash)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        for record in purgeable:
            session.delete(record)
        session.flush()

        grouped = dict(
            session.execute(
                select(ToolGrantRecord.status, func.count()).group_by(
                    ToolGrantRecord.status
                )
            ).all()
        )
        known_total = sum(
            grouped.get(status, 0) for status in ("active", "expired", "revoked")
        )
        total = sum(grouped.values())
        return {
            "active": grouped.get("active", 0),
            "expired": grouped.get("expired", 0),
            "revoked": grouped.get("revoked", 0),
            "other": total - known_total,
            "total": total,
            "expired_marked": len(due),
            "purged": len(purgeable),
            "retention_seconds": retention_seconds,
            "cleanup_batch_size": batch_size,
        }
