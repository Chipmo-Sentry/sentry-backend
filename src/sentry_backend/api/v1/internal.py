"""Internal endpoints — service-to-service (e.g. sentry-ai → backend)."""

import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.service import require_service_token
from sentry_backend.repository import alert_repo
from sentry_backend.schemas.alert import AlertCreateInternal, AlertPublic
from sentry_backend.security import decode_user_token
from sentry_backend.services import alert_notify
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level
from sentry_backend.services.live_broker import get_live_broker
from sentry_backend.services.threshold_handler import get_threshold_handler
from sentry_backend.settings import get_settings

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
    await alert_notify.notify_alert(db, alert)

    return AlertPublic.model_validate(alert)


# ===== M1-LIVE L3: live metadata fanout =====


class LiveTrack(BaseModel):
    """One detected person in one frame (REV.1 — mirrors sentry-ai's
    TrackPayload). Lenient: unknown keys ignored, display fields defaulted so a
    schema addition on the AI side never 422s the whole batch."""

    model_config = ConfigDict(extra="ignore")

    person_id: int
    box: tuple[float, float, float, float]
    det_confidence: float = 0.0
    # Accumulated risk score (absolute, not percent — see /api/v1/behaviors).
    risk_pct: float = Field(default=0.0, ge=0.0)
    color: Literal["green", "yellow", "red"] = "green"


class LiveFrame(BaseModel):
    """Per-analyzed-frame metadata from the sentry-ai live worker
    (REV.1 — mirrors sentry-ai's FrameMetadata)."""

    model_config = ConfigDict(extra="ignore")

    camera_id: str = Field(min_length=1, max_length=128)
    frame_id: int = 0
    ts_ms: int = 0
    width: int = 0
    height: int = 0
    fps_inference: float = 0.0
    tracks: list[LiveTrack] = Field(default_factory=list)


class LiveMetadataBatch(BaseModel):
    """Batch of per-frame metadata from sentry-ai live worker."""

    frames: list[LiveFrame] = Field(min_length=1, max_length=200)


async def _require_simple_internal_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Simpler shared-secret auth for high-volume live metadata.

    sentry-ai sends `Authorization: Bearer <token>`. We accept any of:
      - A bearer token matching `live_metadata_shared_secret` (env), OR
      - A paired node's ai_node JWT (typ=ai_node) — signature + exp only, no
        per-batch DB hit on this high-volume path (revocation is enforced on the
        management endpoints), OR
      - A valid full service JWT (delegates to require_service_token).
    """
    settings = get_settings()
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(maxsplit=1)[1]
        expected = settings.live_metadata_shared_secret
        if expected and hmac.compare_digest(token, expected):
            return
        # Paired AI node token (signature-validated; cheap, no DB).
        try:
            if decode_user_token(token).get("typ") == "ai_node":
                return
        except ValueError:
            pass
    # Fall back to JWT path
    await require_service_token(authorization)


@router.post("/live-metadata", status_code=status.HTTP_202_ACCEPTED)
async def receive_live_metadata(
    body: LiveMetadataBatch,
    _: Annotated[None, Depends(_require_simple_internal_token)],
) -> dict[str, int]:
    """sentry-ai live worker POSTs batched per-frame metadata here.

    Each frame is (a) dispatched to subscribers of its `camera_id` (browser
    WebSocket clients on `/ws/live/{camera_id}`) and (b) fed through the
    threshold handler to fire alerts on sustained risk_pct breaches (L5).
    """
    broker = get_live_broker()
    threshold_handler = get_threshold_handler()
    published = 0
    for frame in body.frames:
        # Downstream (WS fanout + threshold handler) consume the raw dict shape,
        # so dump back to JSON-mode dict — the wire payload to browsers is
        # unchanged, but the batch is now validated/typed on the way in.
        frame_dict = frame.model_dump(mode="json")
        await broker.publish(frame.camera_id, frame_dict)
        await threshold_handler.on_frame(frame_dict)
        published += 1
    return {"received": len(body.frames), "published": published}
