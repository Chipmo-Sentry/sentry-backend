"""Internal endpoints — service-to-service (e.g. sentry-ai → backend)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.service import require_service_token
from sentry_backend.repository import alert_repo
from sentry_backend.schemas.alert import AlertCreateInternal, AlertPublic
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


@router.post("/alerts", response_model=AlertPublic, status_code=status.HTTP_201_CREATED)
async def create_alert_from_ai(
    body: AlertCreateInternal,
    db: Annotated[AsyncSession, Depends(get_db)],
    service_name: Annotated[str, Depends(require_service_token)],
) -> AlertPublic:
    """sentry-ai POSTs here after VLM verification finishes.

    We derive store/camera/org from the clip — sentry-ai doesn't need to
    know the tenancy hierarchy.
    """
    if service_name != "sentry-ai":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Service '{service_name}' not authorized for this endpoint",
        )

    # Look up clip without org scope (service-level access)
    from sqlalchemy import select

    from sentry_backend.db.models.clip import Clip

    result = await db.execute(select(Clip).where(Clip.id == body.clip_id))
    clip = result.scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    alert_level = derive_alert_level(body.category, body.confidence)

    alert = await alert_repo.create_alert(
        db,
        clip_id=body.clip_id,
        organization_id=clip.organization_id,
        store_id=clip.store_id,
        camera_id=clip.camera_id,
        category=body.category,
        confidence=body.confidence,
        reasoning=body.reasoning,
        model_name=body.model_name,
        alert_level=alert_level,
        inference_latency_ms=body.inference_latency_ms,
    )

    # Commit must happen BEFORE publishing — otherwise SSE clients receive
    # an alert ID that briefly doesn't exist in the DB.
    await db.commit()

    payload = AlertPublic.model_validate(alert).model_dump(mode="json")
    await get_broker().publish(clip.organization_id, payload)

    return AlertPublic.model_validate(alert)
