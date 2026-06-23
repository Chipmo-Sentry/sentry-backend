"""Global edge-config persistence (ADR-0029 I3, made global per founder request).

The edge Stage-1 tunables are ONE platform-wide config applied to every store's
agents — not per store. Stored in the shared ``app_config`` singleton table under
``key='edge_config'`` (same pattern as the global behaviour catalog), as
``{"version": N, "overrides": {...}}``. The version bumps on every change so the
store agents' config-poller re-applies only on a real change.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.app_config import AppConfig

GLOBAL_EDGE_KEY = "edge_config"


async def get_global_row(db: AsyncSession) -> AppConfig | None:
    """The single global edge-config row, or None if it was never set."""
    result = await db.execute(select(AppConfig).where(AppConfig.key == GLOBAL_EDGE_KEY))
    return result.scalar_one_or_none()


def parse_row(row: AppConfig | None) -> tuple[int, dict[str, Any]]:
    """(version, overrides) from a row — (1, {}) when unset, so an agent served no
    config gets version 1 + the agent defaults."""
    if row is None:
        return 1, {}
    value = row.value or {}
    version = int(value.get("version", 1))
    overrides = dict(value.get("overrides", {}))
    return version, overrides


async def set_global(db: AsyncSession, overrides: dict[str, Any]) -> AppConfig:
    """Upsert the global overrides and bump ``version`` so all agents re-apply. A
    new row starts at version 2 — a detectable change from the version-1 defaults
    served when no row exists."""
    row = await get_global_row(db)
    if row is None:
        row = AppConfig(key=GLOBAL_EDGE_KEY, value={"version": 2, "overrides": overrides})
        db.add(row)
    else:
        version = int((row.value or {}).get("version", 1)) + 1
        row.value = {"version": version, "overrides": overrides}
    await db.flush()
    return row
