"""Per-store edge-config persistence (ADR-0029 I3)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.edge_config import StoreEdgeConfig


async def get_for_store(db: AsyncSession, store_id: UUID) -> StoreEdgeConfig | None:
    result = await db.execute(select(StoreEdgeConfig).where(StoreEdgeConfig.store_id == store_id))
    return result.scalar_one_or_none()


async def set_overrides(
    db: AsyncSession, store_id: UUID, overrides: dict[str, Any]
) -> StoreEdgeConfig:
    """Upsert the store's overrides and bump ``version`` so agents re-apply. A new
    row starts at version 2 — a detectable change from the version-1 defaults a
    store with no row serves."""
    row = await get_for_store(db, store_id)
    if row is None:
        row = StoreEdgeConfig(store_id=store_id, version=2, overrides=overrides)
        db.add(row)
    else:
        row.overrides = overrides
        row.version += 1
    await db.flush()
    return row
