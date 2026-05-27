"""Alerts router — list + get + SSE stream (read-only; creation via internal endpoint)."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.alert import AlertLevel
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import alert_repo
from sentry_backend.schemas.alert import AlertPublic
from sentry_backend.services.alert_broker import get_broker

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

_SSE_KEEPALIVE_SEC = 15.0


@router.get("", response_model=list[AlertPublic])
async def list_alerts(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    store_id: Annotated[UUID | None, Query()] = None,
    camera_id: Annotated[UUID | None, Query()] = None,
    min_level: Annotated[AlertLevel | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AlertPublic]:
    alerts = await alert_repo.list_alerts_for_org(
        db,
        org_id,
        store_id=store_id,
        camera_id=camera_id,
        min_level=min_level,
        limit=limit,
        offset=offset,
    )
    return [AlertPublic.model_validate(a) for a in alerts]


@router.get("/stream")
async def alert_stream(
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> StreamingResponse:
    """SSE stream of new alerts for the current org.

    Frontend usage:
        const es = new EventSource('/api/v1/alerts/stream', { withCredentials: true });
        es.addEventListener('alert', e => { const data = JSON.parse(e.data); ... });
    """
    broker = get_broker()
    q = await broker.subscribe(org_id)

    async def event_generator() -> AsyncIterator[bytes]:
        try:
            yield b": connected\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=_SSE_KEEPALIVE_SEC)
                    chunk = f"event: alert\ndata: {json.dumps(payload, default=str)}\n\n"
                    yield chunk.encode("utf-8")
                except TimeoutError:
                    yield b": keep-alive\n\n"
        finally:
            await broker.unsubscribe(org_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/{alert_id}", response_model=AlertPublic)
async def get_alert(
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> AlertPublic:
    alert = await alert_repo.get_alert_for_org(db, alert_id, org_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    return AlertPublic.model_validate(alert)
