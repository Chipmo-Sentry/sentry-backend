"""Camera CRUD — joins via Store for org-scoping."""

import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.store import Store
from sentry_backend.security import encrypt_secret


async def list_cameras_for_org(
    db: AsyncSession,
    org_id: UUID,
    store_id: UUID | None = None,
) -> list[Camera]:
    stmt = (
        select(Camera)
        .join(Store, Camera.store_id == Store.id)
        .where(Store.organization_id == org_id)
    )
    if store_id is not None:
        stmt = stmt.where(Camera.store_id == store_id)
    result = await db.execute(stmt.order_by(Camera.name))
    return list(result.scalars().all())


async def get_camera_for_org(db: AsyncSession, camera_id: UUID, org_id: UUID) -> Camera | None:
    stmt = (
        select(Camera)
        .join(Store, Camera.store_id == Store.id)
        .where(Camera.id == camera_id, Store.organization_id == org_id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def count_cameras_for_store(db: AsyncSession, store_id: UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(Camera).where(Camera.store_id == store_id)
    )
    return int(result.scalar_one())


async def store_belongs_to_org(db: AsyncSession, store_id: UUID, org_id: UUID) -> bool:
    result = await db.execute(
        select(Store.id).where(Store.id == store_id, Store.organization_id == org_id)
    )
    return result.scalar_one_or_none() is not None


async def get_camera_by_store_path(
    db: AsyncSession, store_id: UUID, mediamtx_path: str
) -> Camera | None:
    """The store's camera on this mediamtx_path, or None. Lets the agent re-register
    the SAME camera (same path) as an UPDATE instead of churning a new row — so its
    compute_tier / zones survive a re-register."""
    result = await db.execute(
        select(Camera).where(Camera.store_id == store_id, Camera.mediamtx_path == mediamtx_path)
    )
    return result.scalar_one_or_none()


async def create_camera(
    db: AsyncSession,
    *,
    store_id: UUID,
    org_id: UUID,
    name: str,
    rtsp_url: str | None,
    shelf_zone_json: dict[str, Any] | None,
    stage2_threshold: float,
    enabled: bool,
    mediamtx_path: str | None = None,
    risk_threshold: float = 11.0,
    compute_tier: str = "cloud",
    zones: list[dict[str, Any]] | None = None,
) -> Camera | None:
    """Return None if the store doesn't belong to the org (404 surfaced by caller).

    If `mediamtx_path` is None, generate from `name`. If the chosen slug collides
    with an existing camera, append numeric suffix until unique.
    """
    if not await store_belongs_to_org(db, store_id, org_id):
        return None

    path = await _allocate_mediamtx_path(db, mediamtx_path or _slugify(name))

    cam = Camera(
        store_id=store_id,
        name=name,
        rtsp_url_encrypted=encrypt_secret(rtsp_url) if rtsp_url else None,
        shelf_zone_json=shelf_zone_json,
        stage2_threshold=stage2_threshold,
        enabled=enabled,
        mediamtx_path=path,
        risk_threshold=risk_threshold,
        compute_tier=compute_tier,
        zones=zones or None,
    )
    db.add(cam)
    await db.flush()
    return cam


def _slugify(text: str) -> str:
    """ASCII-only slug for mediamtx_path. Falls back to 'cam' if all stripped."""
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")
    return s[:50] or "cam"


async def _allocate_mediamtx_path(db: AsyncSession, preferred: str) -> str:
    """Return `preferred` if unused; otherwise append _2, _3, ... until unique."""
    taken_rows = await db.execute(
        select(Camera.mediamtx_path).where(Camera.mediamtx_path.is_not(None))
    )
    taken = {row[0] for row in taken_rows.all()}
    if preferred not in taken:
        return preferred
    i = 2
    while True:
        candidate = f"{preferred}_{i}"
        if candidate not in taken:
            return candidate
        i += 1


async def update_camera(
    db: AsyncSession,
    camera: Camera,
    *,
    name: str | None = None,
    rtsp_url: str | None = None,
    shelf_zone_json: dict[str, Any] | None = None,
    stage2_threshold: float | None = None,
    enabled: bool | None = None,
    mediamtx_path: str | None = None,
    risk_threshold: float | None = None,
    compute_tier: str | None = None,
    zones: list[dict[str, Any]] | None = None,
) -> Camera:
    if name is not None:
        camera.name = name
    if rtsp_url is not None:
        camera.rtsp_url_encrypted = encrypt_secret(rtsp_url)
    if shelf_zone_json is not None:
        camera.shelf_zone_json = shelf_zone_json
    if stage2_threshold is not None:
        camera.stage2_threshold = stage2_threshold
    if enabled is not None:
        camera.enabled = enabled
    if mediamtx_path is not None and mediamtx_path != camera.mediamtx_path:
        # Ensure uniqueness on update too
        camera.mediamtx_path = await _allocate_mediamtx_path(db, mediamtx_path)
    if risk_threshold is not None:
        camera.risk_threshold = risk_threshold
    if compute_tier is not None:
        camera.compute_tier = compute_tier
    if zones is not None:
        camera.zones = zones or None  # [] = clear all zones → store null; None = no-op
    await db.flush()
    return camera


async def decrypt_rtsp_url(camera: Camera) -> str | None:
    """Helper: decrypt the stored RTSP URL (sync, no DB).

    Returns None if the camera has no stored URL.
    """
    from sentry_backend.security import decrypt_secret

    if camera.rtsp_url_encrypted is None:
        return None
    return decrypt_secret(camera.rtsp_url_encrypted)


async def delete_camera(db: AsyncSession, camera: Camera) -> None:
    await db.delete(camera)
