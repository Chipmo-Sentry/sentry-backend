"""WebSocket /ws/live/{camera_id} — per-camera live metadata fanout to browser.

Cookie auth (same `sentry_access` cookie as REST endpoints). The browser
WebSocket API sends cookies automatically for same-origin / SameSite-compatible
cross-origin connections, so no client-side header config needed.

For M1 simplicity, tenancy (org → camera) is NOT enforced — any authenticated
user can subscribe to any camera_id. M2 wires per-camera org check.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentry_backend.db.session import get_sessionmaker
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository.user_repo import get_user_by_id
from sentry_backend.security import decode_user_token
from sentry_backend.services.live_broker import get_live_broker

router = APIRouter(tags=["live"])
log = get_logger("sentry_backend.ws_live")


async def _resolve_user_from_token(
    sm: async_sessionmaker[AsyncSession], token: str
) -> tuple[bool, str | None]:
    """Validate access token + return (is_valid, user_id_str)."""
    try:
        payload = decode_user_token(token)
    except ValueError:
        return False, None
    if payload.get("typ") != "access":
        return False, None
    sub = payload.get("sub")
    if not isinstance(sub, str):
        return False, None
    from uuid import UUID

    try:
        user_id = UUID(sub)
    except ValueError:
        return False, None
    async with sm() as session:
        user = await get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        return False, None
    return True, sub


@router.websocket("/ws/live/{camera_id}")
async def ws_live(
    websocket: WebSocket,
    camera_id: str,
    token: str | None = Query(
        default=None,
        description="Access token fallback for native WebSocket clients that "
        "can't send cookies (e.g. some mobile envs).",
    ),
) -> None:
    # Auth — prefer cookie (browser default), fall back to ?token= query param
    sentry_access = websocket.cookies.get("sentry_access") or token
    if not sentry_access:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="no auth")
        return

    sm = get_sessionmaker()
    ok, user_sub = await _resolve_user_from_token(sm, sentry_access)
    if not ok:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
        return

    broker = get_live_broker()
    queue = await broker.subscribe(camera_id)
    await websocket.accept()
    log.info("ws_live.connected", camera_id=camera_id, user=user_sub)

    try:
        # Drain queue → client.  Client→server messages aren't used in L3 but we
        # still need a parallel receive task to detect disconnect promptly.
        async def _client_receiver() -> None:
            try:
                while True:
                    # Discard any incoming text (no protocol for client→server yet)
                    await websocket.receive_text()
            except WebSocketDisconnect:
                return

        receiver_task = asyncio.create_task(_client_receiver())
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    await websocket.send_json(payload)
                except TimeoutError:
                    # heartbeat — also helps detect dead connections
                    await websocket.send_json({"_type": "heartbeat"})
                if receiver_task.done():
                    break
        finally:
            receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver_task
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws_live.error", camera_id=camera_id)
    finally:
        await broker.unsubscribe(camera_id, queue)
        log.info("ws_live.disconnected", camera_id=camera_id, user=user_sub)
