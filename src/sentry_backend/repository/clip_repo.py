"""Clip CRUD."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.clip import Clip


async def get_clip_for_org(db: AsyncSession, clip_id: UUID, org_id: UUID) -> Clip | None:
    result = await db.execute(
        select(Clip).where(Clip.id == clip_id, Clip.organization_id == org_id)
    )
    return result.scalar_one_or_none()


async def find_clip_by_sha256_for_org(db: AsyncSession, sha256: str, org_id: UUID) -> Clip | None:
    result = await db.execute(
        select(Clip).where(Clip.sha256 == sha256, Clip.organization_id == org_id)
    )
    return result.scalar_one_or_none()


async def list_clips_for_org(
    db: AsyncSession,
    org_id: UUID,
    *,
    store_id: UUID | None = None,
    camera_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Clip]:
    stmt = select(Clip).where(Clip.organization_id == org_id)
    if store_id is not None:
        stmt = stmt.where(Clip.store_id == store_id)
    if camera_id is not None:
        stmt = stmt.where(Clip.camera_id == camera_id)
    stmt = stmt.order_by(Clip.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_clip(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID | None,
    camera_id: UUID | None,
    captured_at: datetime,
    duration_sec: float,
    storage_path: str,
    file_size_bytes: int,
    sha256: str,
    edge_behavior_detail: list[dict[str, Any]] | None = None,
    edge_risk_pct: float | None = None,
) -> Clip:
    clip = Clip(
        organization_id=organization_id,
        store_id=store_id,
        camera_id=camera_id,
        captured_at=captured_at,
        duration_sec=duration_sec,
        storage_path=storage_path,
        file_size_bytes=file_size_bytes,
        sha256=sha256,
        edge_behavior_detail=edge_behavior_detail,
        edge_risk_pct=edge_risk_pct,
    )
    db.add(clip)
    await db.flush()
    return clip
