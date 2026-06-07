"""Provision/deprovision a camera's full live pipeline in one call.

Wraps the two best-effort side effects that every camera mutation needs:
  1. MediaMTX path (video fan-out: WebRTC/HLS to clients, RTSP to AI)
  2. sentry-ai live worker (detection metadata → /ws/live/{path})

Keeping both behind one function means cameras.py (user CRUD) and agents.py
(agent CRUD) stay in sync — neither can forget to start the AI worker.
All calls are best-effort and never raise.
"""

from __future__ import annotations

from sentry_backend.logging_setup import get_logger
from sentry_backend.services import live_ai_client, mediamtx_client

log = get_logger("sentry_backend.live_provision")


async def provision(
    mediamtx_path: str, camera_rtsp: str, *, enabled: bool, store_id: str | None = None
) -> None:
    """Bring a camera online (or take it offline if disabled).

    Enabled  → add MediaMTX path (pulls the camera RTSP) + start AI worker.
    Disabled → tear both down. `store_id` enables cross-camera re-ID (ADR-0023).
    """
    if not enabled:
        await deprovision(mediamtx_path)
        return
    await mediamtx_client.add_path(mediamtx_path, camera_rtsp)
    # MediaMTX needs the path to exist before the AI worker can pull from it;
    # add_path returned, so the path config is registered. The worker retries
    # its RTSP connection internally if MediaMTX isn't serving frames yet.
    await live_ai_client.start_worker(mediamtx_path, store_id=store_id)


async def deprovision(mediamtx_path: str) -> None:
    """Stop the AI worker and remove the MediaMTX path."""
    await live_ai_client.stop_worker(mediamtx_path)
    await mediamtx_client.delete_path(mediamtx_path)
