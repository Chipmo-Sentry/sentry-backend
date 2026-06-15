"""emit() — the single entry point for writing an EventLog row + SSE fanout.

Callers pass the request's own AsyncSession; the insert happens inside a
SAVEPOINT so a logging failure can never poison the caller's transaction or
fail the real action (login still succeeds even if its audit row doesn't). On
success the row is published to the in-process event bus for live SSE streams.

Messages are Mongolian (they render verbatim in the Logs UI).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.event_log import EventLog, EventSeverity, EventType
from sentry_backend.logging_setup import get_logger
from sentry_backend.schemas.event_log import EventLogPublic
from sentry_backend.services.event_bus import get_event_bus

log = get_logger("sentry_backend.event_log")

# Heartbeats are "still alive" pings; keep only a recent window so the table
# stays bounded. Non-heartbeat events are never auto-pruned.
HEARTBEAT_RETENTION_SEC = 24 * 3600


async def emit(
    db: AsyncSession,
    *,
    event_type: EventType,
    message: str,
    severity: EventSeverity = EventSeverity.info,
    organization_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    actor_label: str | None = None,
    store_id: UUID | None = None,
    camera_id: UUID | None = None,
    agent_id: UUID | None = None,
    ai_node_id: UUID | None = None,
    detail: dict[str, Any] | None = None,
    is_heartbeat: bool = False,
) -> None:
    """Best-effort: never raises. Writes one EventLog row + fans out via SSE."""
    row = EventLog(
        event_type=event_type,
        severity=severity,
        message=message,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        store_id=store_id,
        camera_id=camera_id,
        agent_id=agent_id,
        ai_node_id=ai_node_id,
        detail=detail,
        is_heartbeat=is_heartbeat,
    )
    try:
        # SAVEPOINT — isolates this insert from the caller's transaction.
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except Exception:  # noqa: BLE001 — audit logging must never break the action
        log.warning("event_log_emit_failed", event_type=event_type.value, exc_info=True)
        return

    try:
        payload = EventLogPublic.model_validate(row).model_dump(mode="json")
        await get_event_bus().publish(organization_id, payload)
    except Exception:  # noqa: BLE001 — fanout is best-effort
        log.warning("event_log_publish_failed", event_type=event_type.value, exc_info=True)


async def prune_heartbeats(db: AsyncSession, *, older_than_seconds: int) -> int:
    """Delete heartbeat rows older than the cutoff. Returns rows removed.

    Heartbeats are high-volume "still alive" pings; we keep only a recent window
    so the table doesn't grow unbounded. Non-heartbeat events are never pruned.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    result = await db.execute(
        delete(EventLog).where(
            EventLog.is_heartbeat.is_(True),
            EventLog.created_at < cutoff,
        )
    )
    # execute() is typed as the generic Result (no .rowcount), but a DELETE
    # really returns a CursorResult that has it (mirrors retention._rowcount).
    return int(getattr(result, "rowcount", 0) or 0)
