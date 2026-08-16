"""Store (physical retail location)."""

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Store(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stores"

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Ulaanbaatar", nullable=False)
    # BE1: per-store Telegram chat to ping on actionable alerts. None → fall
    # back to the global TELEGRAM_ALERT_CHAT_ID (or no notification).
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Where this store's agent pushes its camera streams (cloud topology), e.g.
    # "rtsp://116.127.115.18:8554". None → fall back to the global
    # AGENT_STREAM_PUSH_URL env. Editable from superadmin so a vast.ai instance
    # restart (new IP/port) can be fixed without a backend redeploy.
    agent_stream_push_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Named cloudflared tunnel for THIS store's agent (stable agent-direct video
    # URL). token → `cloudflared tunnel run --token …` on the agent; hostname →
    # the public HTTPS base it serves (e.g. "store1.chipmo.mn"). Both None →
    # the agent falls back to an ephemeral quick tunnel. Superadmin-editable.
    agent_tunnel_token: Mapped[str | None] = mapped_column(String(500), nullable=True)
    agent_tunnel_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # docs/30 — top-down floor plan authored in the agent-pc «Plan зураг» page:
    # {size, walls, fixtures[type], cameras[pos, dir_deg, homography, calib_points]}.
    # The plan is the SOURCE; per-camera Camera.zones are DERIVED from it (plan
    # fixture → camera homography → normalized polygon). Additive; null = none yet.
    floor_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
