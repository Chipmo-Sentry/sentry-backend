"""Queries for the event/activity log."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.event_log import EventLog, EventSeverity, EventType


async def list_events(
    db: AsyncSession,
    *,
    organization_id: UUID | None,
    scope_to_org: bool,
    event_types: Sequence[EventType] | None = None,
    severities: Sequence[EventSeverity] | None = None,
    include_heartbeats: bool = False,
    before: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[EventLog]:
    """Newest-first event page.

    scope_to_org=True  → strictly events for `organization_id` (org-admin view;
                         platform-level NULL-org rows are excluded).
    scope_to_org=False → super-admin view: all events, optionally narrowed to
                         `organization_id` when it is provided.
    """
    stmt = select(EventLog)
    # scope_to_org → always filter to that org (org-admin view). Super-admin view
    # (scope_to_org False) filters only when an org_id narrows the query.
    if scope_to_org or organization_id is not None:
        stmt = stmt.where(EventLog.organization_id == organization_id)

    if event_types:
        stmt = stmt.where(EventLog.event_type.in_(event_types))
    if severities:
        stmt = stmt.where(EventLog.severity.in_(severities))
    if not include_heartbeats:
        stmt = stmt.where(EventLog.is_heartbeat.is_(False))
    if before is not None:
        stmt = stmt.where(EventLog.created_at < before)

    stmt = stmt.order_by(EventLog.created_at.desc(), EventLog.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())
