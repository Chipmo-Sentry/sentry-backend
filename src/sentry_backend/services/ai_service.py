"""Background trigger: send a freshly-uploaded clip to sentry-ai, store
the returned alert, and push to SSE subscribers.

M1 flow (laptop, single-process):
    POST /api/v1/clips → creates Clip → asyncio.create_task(verify_clip_with_ai)
    │                                                       │
    │ returns ClipPublic immediately                        │ background
    │                                                       ▼
    │                                              httpx POST sentry-ai /v1/verify
    │                                                       │ waits ~5-15s
    │                                                       ▼
    │                                              derive_alert_level + insert Alert
    │                                                       │
    │                                                       ▼
    │                                              broker.publish → SSE clients

M2+ migration: sentry-ai posts to /api/v1/internal/alerts instead (already
exists, uses service-token auth), so backend doesn't have to wait for VLM.
For M1 the simpler sync-from-backend model is fine."""

from uuid import UUID

import httpx
from sqlalchemy import select

from sentry_backend.db.models.alert import AlertCategory
from sentry_backend.db.models.clip import Clip
from sentry_backend.db.session import session_scope
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import alert_repo
from sentry_backend.schemas.alert import AlertPublic
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.ai_service")


async def verify_clip_with_ai(clip_id: UUID) -> None:
    """Background task — never raises; logs failures.

    Detached from the request lifecycle on purpose: the user's upload
    response already came back; if sentry-ai is unreachable we just log
    and leave the clip unverified (a manual /v1/verify retry could re-run
    it later — Session 3 work).
    """
    settings = get_settings()
    if not settings.sentry_ai_url:
        log.warning("sentry_ai_url not configured, skipping verify", clip_id=str(clip_id))
        return

    try:
        await _verify_inner(clip_id, settings.sentry_ai_url, settings.sentry_ai_timeout_sec)
    except Exception as e:  # noqa: BLE001 — background tasks must never raise
        log.exception("verify task crashed", clip_id=str(clip_id), error=str(e))


async def _verify_inner(clip_id: UUID, ai_base_url: str, timeout_sec: int) -> None:
    """The actual work, wrapped by the catch-all above."""
    # 1. Load the clip metadata so we have the on-disk path
    clip_payload: dict[str, object] | None = None
    storage_path: str | None = None
    async with session_scope() as db:
        clip = (await db.execute(select(Clip).where(Clip.id == clip_id))).scalar_one_or_none()
        if clip is None:
            log.error("clip not found", clip_id=str(clip_id))
            return
        storage_path = clip.storage_path
        clip_payload = {
            "store_id": str(clip.store_id) if clip.store_id else None,
            "camera_id": str(clip.camera_id) if clip.camera_id else None,
            "organization_id": clip.organization_id,
        }

    # 2. Call sentry-ai /v1/verify (synchronous from our POV, but already
    # out of the user's request path).
    body = {
        "clip_path": storage_path,
        "store_id": clip_payload["store_id"],
        "camera_id": clip_payload["camera_id"],
    }
    log.info("calling sentry-ai", clip_id=str(clip_id), url=f"{ai_base_url}/v1/verify")
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        resp = await client.post(f"{ai_base_url}/v1/verify", json=body)

    if resp.status_code != 200:
        log.error(
            "sentry-ai non-200", clip_id=str(clip_id), status=resp.status_code, body=resp.text[:500]
        )
        return

    data = resp.json()

    # 3. Insert Alert
    try:
        category = AlertCategory(data["category"])
    except (KeyError, ValueError) as e:
        log.error("invalid category from sentry-ai", clip_id=str(clip_id), data=data, error=str(e))
        return

    alert_level = derive_alert_level(category, float(data["confidence"]))

    alert_public: AlertPublic | None = None
    async with session_scope() as db:
        alert = await alert_repo.create_alert(
            db,
            clip_id=clip_id,
            organization_id=clip_payload["organization_id"],  # type: ignore[arg-type]
            store_id=UUID(clip_payload["store_id"]) if clip_payload["store_id"] else None,  # type: ignore[arg-type]
            camera_id=UUID(clip_payload["camera_id"]) if clip_payload["camera_id"] else None,  # type: ignore[arg-type]
            category=category,
            confidence=float(data["confidence"]),
            reasoning=str(data["reasoning"]),
            model_name=str(data["model_name"]),
            alert_level=alert_level,
            inference_latency_ms=int(data["inference_latency_ms"]),
        )
        # session_scope auto-commits on exit
        alert_public = AlertPublic.model_validate(alert)

    # 4. Push to SSE subscribers (outside DB session)
    if alert_public is not None:
        await get_broker().publish(
            alert_public.organization_id,
            alert_public.model_dump(mode="json"),
        )
        log.info(
            "alert published",
            clip_id=str(clip_id),
            alert_id=str(alert_public.id),
            category=str(category),
            level=str(alert_level),
        )
