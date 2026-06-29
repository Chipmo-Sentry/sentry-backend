"""WebSocket /ws/live/{camera_id} — per-camera live metadata fanout to browser.

Cookie auth (same `sentry_access` cookie as REST endpoints). The browser
WebSocket API sends cookies automatically for same-origin / SameSite-compatible
cross-origin connections, so no client-side header config needed.

Tenancy: the camera_id in the path is the Camera.mediamtx_path. We resolve it
to its org (Camera → Store → Organization) and require the user to be a member
of that org (super-admins pass). Unknown camera / non-member → 1008 close.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.organization import OrganizationMember
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.db.session import get_sessionmaker
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository.user_repo import get_user_by_id
from sentry_backend.security import decode_user_token
from sentry_backend.services.live_broker import get_live_broker

router = APIRouter(tags=["live"])
log = get_logger("sentry_backend.ws_live")


async def _resolve_user_from_token(sm: async_sessionmaker[AsyncSession], token: str) -> User | None:
    """Validate access token + return the User, or None if invalid/inactive."""
    try:
        payload = decode_user_token(token)
    except ValueError:
        return None
    if payload.get("typ") != "access":
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str):
        return None
    try:
        user_id = UUID(sub)
    except ValueError:
        return None
    async with sm() as session:
        user = await get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        return None
    return user


async def _user_may_view_camera(
    sm: async_sessionmaker[AsyncSession], user: User, mediamtx_path: str
) -> bool:
    """True if the user's org owns the camera (or user is super-admin).

    Resolves Camera (by mediamtx_path) → Store → org, then checks membership.
    Unknown camera → False (don't leak existence to non-owners).
    """
    if user.is_super_admin:
        return True
    async with sm() as session:
        org_id = (
            await session.execute(
                select(Store.organization_id)
                .join(Camera, Camera.store_id == Store.id)
                .where(Camera.mediamtx_path == mediamtx_path)
            )
        ).scalar_one_or_none()
        if org_id is None:
            return False
        is_member = (
            await session.execute(
                select(OrganizationMember.user_id).where(
                    OrganizationMember.user_id == user.id,
                    OrganizationMember.organization_id == org_id,
                )
            )
        ).scalar_one_or_none()
        return is_member is not None


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
    user = await _resolve_user_from_token(sm, sentry_access)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
        return

    # Tenancy: camera_id == Camera.mediamtx_path; require org membership.
    if not await _user_may_view_camera(sm, user, camera_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="forbidden")
        return
    user_sub = str(user.id)

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
            # Loop while the client is connected. Check `receiver_task.done()` up
            # front so we never try to send on a socket the client already closed.
            while not receiver_task.done():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # No overlay metadata for 15s — the AI node / edge agent isn't
                    # posting tracks (node offline, or no people in frame). Heartbeat
                    # keeps the socket alive; the video itself comes over HLS, so an
                    # empty overlay is not an error.
                    payload = {"_type": "heartbeat"}
                try:
                    await websocket.send_json(payload)
                except (WebSocketDisconnect, RuntimeError):
                    # Client vanished between our done()-check and the send (ASGI
                    # raises RuntimeError when sending after the socket closed). A
                    # normal disconnect — exit cleanly, don't log it as an error.
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
