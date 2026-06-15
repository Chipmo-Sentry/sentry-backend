"""EventLog — a unified, queryable activity/audit timeline.

Until now events lived only in structlog (not queryable) or as domain rows
(Alert, Agent.last_seen_at, …) with no single timeline. This table is the one
place every notable thing lands: a user logs in, a camera connects, the AI
flags suspicious activity, a desktop agent goes offline, an error fires.

Scope: `organization_id` is NULL for platform-level events (visible to
super-admins only); org-scoped rows are visible to that org's admins and to
super-admins. Timestamps are PostgreSQL `timestamptz` (microsecond precision) —
the UI renders HH:MM:SS.mmm.

Volume: heartbeats are high-frequency (every few seconds per device). They are
flagged `is_heartbeat=True` so the UI can filter them out by default and a
retention job can prune them without touching the meaningful events.
"""

import enum
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EventType(enum.StrEnum):
    # --- auth / users ---
    user_login = "user_login"
    user_logout = "user_logout"
    user_invited = "user_invited"
    invite_accepted = "invite_accepted"
    member_role_changed = "member_role_changed"
    member_access_changed = "member_access_changed"
    # --- organization ---
    org_created = "org_created"
    org_deleted = "org_deleted"
    # --- camera ---
    camera_registered = "camera_registered"
    camera_updated = "camera_updated"
    camera_stream_down = "camera_stream_down"
    camera_stream_recovered = "camera_stream_recovered"
    # --- desktop agent (agent-pc) ---
    agent_paired = "agent_paired"
    agent_online = "agent_online"
    agent_offline = "agent_offline"
    agent_heartbeat = "agent_heartbeat"
    # --- AI node ---
    node_paired = "node_paired"
    node_online = "node_online"
    node_offline = "node_offline"
    node_heartbeat = "node_heartbeat"
    # --- detection ---
    alert_created = "alert_created"
    # A person's live episode ended — peak risk + which behaviours fired, even
    # when it never crossed the alert threshold (visibility into live scoring).
    risk_episode = "risk_episode"
    # --- generic ---
    error = "error"


class EventSeverity(enum.StrEnum):
    info = "info"
    success = "success"
    warning = "warning"
    error = "error"
    critical = "critical"


class EventLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "event_logs"
    __table_args__ = (
        # Org timeline, newest-first — the org-admin Logs page hot path.
        Index("ix_event_logs_org_created_at", "organization_id", "created_at"),
        # Platform timeline (super-admin) + type filtering.
        Index("ix_event_logs_created_at", "created_at"),
        Index("ix_event_logs_type_created_at", "event_type", "created_at"),
        # Retention prune of high-volume heartbeat rows by age.
        Index("ix_event_logs_heartbeat_created_at", "is_heartbeat", "created_at"),
    )

    # NULL = platform-level event (super-admin only).
    organization_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type"), nullable=False
    )
    severity: Mapped[EventSeverity] = mapped_column(
        Enum(EventSeverity, name="event_severity"),
        default=EventSeverity.info,
        server_default=EventSeverity.info.value,
        nullable=False,
    )
    # Human-readable Mongolian summary shown in the timeline row.
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Who/what caused it. actor_user_id is set for human actions; actor_label is
    # a display string for any actor (email, agent name, "Систем").
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Optional subject references (kept loose — no hard FK to agents/nodes so a
    # delete never cascades audit history away unexpectedly).
    store_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    camera_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    agent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    ai_node_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    # Arbitrary structured context (ip, fps, risk_pct, …).
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # High-volume heartbeat marker — hidden by default in the UI, pruned by age.
    is_heartbeat: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
