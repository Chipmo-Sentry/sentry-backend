"""Camera CRUD — joins via Store for org-scoping."""
from typing import Any
from uuid import UUID

from sqlalchemy import select
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


async def get_camera_for_org(
    db: AsyncSession, camera_id: UUID, org_id: UUID
) -> Camera | None:
    stmt = (
        select(Camera)
        .join(Store, Camera.store_id == Store.id)
        .where(Camera.id == camera_id, Store.organization_id == org_id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _store_belongs_to_org(
    db: AsyncSession, store_id: UUID, org_id: UUID
) -> bool:
    result = await db.execute(
        select(Store.id).where(
            Store.id == store_id, Store.organization_id == org_id
        )
    )
    return result.scalar_one_or_none() is not None


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
) -> Camera | None:
    """Return None if the store doesn't belong to the org (404 surfaced by caller)."""
    if not await _store_belongs_to_org(db, store_id, org_id):
        return None
    cam = Camera(
        store_id=store_id,
        name=name,
        rtsp_url_encrypted=encrypt_secret(rtsp_url) if rtsp_url else None,
        shelf_zone_json=shelf_zone_json,
        stage2_threshold=stage2_threshold,
        enabled=enabled,
    )
    db.add(cam)
    await db.flush()
    return cam


async def update_camera(
    db: AsyncSession,
    camera: Camera,
    *,
    name: str | None = None,
    rtsp_url: str | None = None,
    shelf_zone_json: dict[str, Any] | None = None,
    stage2_threshold: float | None = None,
    enabled: bool | None = None,
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
    await db.flush()
    return camera


async def delete_camera(db: AsyncSession, camera: Camera) -> None:
    await db.delete(camera)
