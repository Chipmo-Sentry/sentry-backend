"""Per-store edge Stage-1 tunable overrides (ADR-0029 I3).

The store agent's config-poller fetches the merged config (defaults +
``overrides``) from ``GET /api/v1/agent/edge-config``. ``version`` bumps on every
change so the agent re-applies ONLY when something actually changed. Only the
overridden keys are stored — a removed/renamed tunable silently falls back to the
agent's own default.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin


class StoreEdgeConfig(TimestampMixin, Base):
    # DEPRECATED (2026-06-23): the edge config is now ONE GLOBAL config stored in
    # app_config key='edge_config' (see edge_config_repo) — per the founder's
    # decision to tune all stores with a single value. This table is no longer
    # read or written; it's kept (not dropped) to avoid an Alembic migration in a
    # repo with hand-assigned, collision-prone revision ids. Safe to drop later.
    __tablename__ = "store_edge_config"

    store_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stores.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Monotonic; the agent re-applies the config only when this changes. A store
    # with no row serves the defaults at version 1, so the first override starts
    # at 2 (a detectable change).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    # Partial dict of EdgeConfigPayload tunables — only the overridden keys.
    overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
