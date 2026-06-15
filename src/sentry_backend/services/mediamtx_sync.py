"""Rehydrate MediaMTX path config from the Camera table on backend startup.

MediaMTX (M1 standalone, M2 Hetzner) loses dynamically-added paths on
restart — it only knows the paths in its static mediamtx.yml. After a reboot
the operator otherwise has to re-register every camera. This module pushes all
enabled cameras (with a mediamtx_path + RTSP URL) back to MediaMTX at startup
so the live pipeline recovers automatically.

Best-effort: runs in the lifespan startup, never blocks app boot. If MediaMTX
is unreachable, logs and moves on (camera CRUD still works; a later register
or another restart re-syncs).
"""

from __future__ import annotations

from sqlalchemy import select

from sentry_backend.db.models.camera import Camera
from sentry_backend.db.session import session_scope
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import camera_repo
from sentry_backend.services import live_ai_client, mediamtx_client
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.mediamtx_sync")


async def rehydrate_paths() -> int:
    """Push all enabled cameras with a path + RTSP URL to MediaMTX.

    Returns the number of paths successfully (re)added. No-op if the MediaMTX
    API is disabled (MEDIAMTX_API_URL unset).
    """
    if not get_settings().mediamtx_api_url:
        log.debug("mediamtx_sync.skipped", reason="MEDIAMTX_API_URL not set")
        return 0

    async with session_scope() as db:
        cams = (
            (
                await db.execute(
                    select(Camera).where(
                        Camera.enabled.is_(True),
                        Camera.mediamtx_path.is_not(None),
                        Camera.rtsp_url_encrypted.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        # Decrypt inside the session scope (camera objects still attached)
        to_sync: list[tuple[str, str, str, float]] = []
        for cam in cams:
            rtsp = await camera_repo.decrypt_rtsp_url(cam)
            if cam.mediamtx_path and rtsp:
                to_sync.append((cam.mediamtx_path, rtsp, str(cam.store_id), cam.risk_threshold))

    added = 0
    for path, rtsp, store_id, risk_threshold in to_sync:
        if await mediamtx_client.add_path(path, rtsp):
            added += 1
        # Also restart the AI live worker for this camera (best-effort) so the
        # detection overlay recovers, not just the video stream.
        await live_ai_client.start_worker(path, store_id=store_id, risk_threshold=risk_threshold)
    log.info("mediamtx_sync.rehydrated", total=len(to_sync), added=added)
    return added
