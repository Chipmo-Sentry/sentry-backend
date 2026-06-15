"""Events router — activity/audit timeline (list + live SSE).

Two audiences, one model:
  - org admins: GET /api/v1/events[/stream] — only their org's events.
  - super-admins: GET /api/v1/admin/events[/stream] — every org + platform
    events, optionally narrowed by ?org_id=.

Read-only; rows are written by services.event_log.emit at the source of each
action.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.event_log import EventSeverity, EventType
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import event_log_repo
from sentry_backend.schemas.event_log import EventLogPublic
from sentry_backend.services.event_bus import get_event_bus

router = APIRouter(tags=["events"])

_SSE_KEEPALIVE_SEC = 15.0


async def _list(
    db: AsyncSession,
    *,
    organization_id: UUID | None,
    scope_to_org: bool,
    event_types: list[EventType] | None,
    severities: list[EventSeverity] | None,
    include_heartbeats: bool,
    before: datetime | None,
    limit: int,
    offset: int,
) -> list[EventLogPublic]:
    rows = await event_log_repo.list_events(
        db,
        organization_id=organization_id,
        scope_to_org=scope_to_org,
        event_types=event_types,
        severities=severities,
        include_heartbeats=include_heartbeats,
        before=before,
        limit=limit,
        offset=offset,
    )
    return [EventLogPublic.model_validate(r) for r in rows]


def _sse(org_filter: UUID | None) -> StreamingResponse:
    bus = get_event_bus()

    async def event_generator() -> AsyncIterator[bytes]:
        sub = await bus.subscribe(org_filter)
        try:
            yield b": connected\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(sub.queue.get(), timeout=_SSE_KEEPALIVE_SEC)
                    chunk = f"event: log\ndata: {json.dumps(payload, default=str)}\n\n"
                    yield chunk.encode("utf-8")
                except TimeoutError:
                    yield b": keep-alive\n\n"
        finally:
            await bus.unsubscribe(sub)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Org-admin: only the caller's org ────────────────────────────────────────


@router.get("/api/v1/events", response_model=list[EventLogPublic])
async def list_org_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    event_type: Annotated[list[EventType] | None, Query()] = None,
    severity: Annotated[list[EventSeverity] | None, Query()] = None,
    include_heartbeats: Annotated[bool, Query()] = False,
    before: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventLogPublic]:
    return await _list(
        db,
        organization_id=org_id,
        scope_to_org=True,
        event_types=event_type,
        severities=severity,
        include_heartbeats=include_heartbeats,
        before=before,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/events/stream")
async def stream_org_events(
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> StreamingResponse:
    return _sse(org_id)


# ── Super-admin: all orgs + platform events ─────────────────────────────────


@router.get("/api/v1/admin/events", response_model=list[EventLogPublic])
async def list_admin_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    org_id: Annotated[UUID | None, Query()] = None,
    event_type: Annotated[list[EventType] | None, Query()] = None,
    severity: Annotated[list[EventSeverity] | None, Query()] = None,
    include_heartbeats: Annotated[bool, Query()] = False,
    before: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventLogPublic]:
    return await _list(
        db,
        organization_id=org_id,
        scope_to_org=False,
        event_types=event_type,
        severities=severity,
        include_heartbeats=include_heartbeats,
        before=before,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/admin/events/stream")
async def stream_admin_events(
    _: Annotated[User, Depends(require_super_admin)],
) -> StreamingResponse:
    return _sse(None)
