"""sentry-ai live-worker control — start/stop the per-camera AI worker.

When a camera is enabled, the backend tells sentry-ai to spin up a worker that
pulls frames from MediaMTX (NOT the raw camera) and POSTs detection metadata
back to /api/v1/internal/live-metadata. The worker's `camera_id` is the
camera's `mediamtx_path`, which is exactly what browser clients subscribe to on
/ws/live/{camera_id} — so video (WebRTC) and overlay (WS) line up by construction.

Best-effort: failures never block camera CRUD. If sentry-ai is down the camera
still streams video; the worker can be (re)started on the next register or via
mediamtx_sync rehydrate at backend startup.
"""

from __future__ import annotations

import httpx

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import Settings, get_settings

log = get_logger("sentry_backend.live_ai_client")


def _ai_auth_headers(settings: Settings) -> dict[str, str] | None:
    """Bearer header for sentry-ai's /v1/* routes, or None when no token is set."""
    token = settings.sentry_ai_service_token
    return {"Authorization": f"Bearer {token}"} if token else None


def mediamtx_stream_url(mediamtx_path: str) -> str | None:
    """RTSP URL the AI worker should read for this camera (MediaMTX fan-out).

    Returns None when MEDIAMTX_RTSP_URL is unset (AI auto-start disabled).
    """
    base = get_settings().mediamtx_rtsp_url
    if not base:
        return None
    return f"{base.rstrip('/')}/{mediamtx_path}"


async def start_worker(mediamtx_path: str, store_id: str | None = None) -> bool:
    """POST sentry-ai /v1/live/start for this camera. Never raises.

    `store_id` enables store-scoped cross-camera re-ID on the node (ADR-0023).
    """
    settings = get_settings()
    if not settings.sentry_ai_url or not settings.mediamtx_rtsp_url:
        log.debug("live_ai.disabled", reason="sentry_ai_url or mediamtx_rtsp_url unset")
        return False
    rtsp_url = mediamtx_stream_url(mediamtx_path)
    if rtsp_url is None:
        return False

    url = f"{settings.sentry_ai_url.rstrip('/')}/v1/live/start"
    headers = _ai_auth_headers(settings)
    body = {"camera_id": mediamtx_path, "rtsp_url": rtsp_url, "store_id": store_id}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=body, headers=headers)
        if r.status_code in (200, 202):
            log.info("live_ai.start_ok", camera_id=mediamtx_path)
            return True
        log.warning(
            "live_ai.start_failed", camera_id=mediamtx_path, status=r.status_code, body=r.text[:200]
        )
    except (httpx.HTTPError, TimeoutError) as e:
        log.warning("live_ai.start_http_err", camera_id=mediamtx_path, error=str(e))
    return False


async def stop_worker(mediamtx_path: str) -> bool:
    """POST sentry-ai /v1/live/stop/{camera_id}. Never raises."""
    settings = get_settings()
    if not settings.sentry_ai_url:
        return False
    url = f"{settings.sentry_ai_url.rstrip('/')}/v1/live/stop/{mediamtx_path}"
    headers = _ai_auth_headers(settings)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers=headers)
        if r.status_code in (200, 202, 404):
            log.info("live_ai.stop_ok", camera_id=mediamtx_path, status=r.status_code)
            return True
        log.warning("live_ai.stop_failed", camera_id=mediamtx_path, status=r.status_code)
    except (httpx.HTTPError, TimeoutError) as e:
        log.warning("live_ai.stop_http_err", camera_id=mediamtx_path, error=str(e))
    return False
