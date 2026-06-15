"""event_logs — unified activity/audit timeline

Revision ID: e7f8a9b0c1d2
Revises: d1e2f3a4b5c6
Create Date: 2026-06-15 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPE = sa.Enum(
    "user_login",
    "user_logout",
    "user_invited",
    "invite_accepted",
    "member_role_changed",
    "member_access_changed",
    "org_created",
    "org_deleted",
    "camera_registered",
    "camera_updated",
    "camera_stream_down",
    "camera_stream_recovered",
    "agent_paired",
    "agent_online",
    "agent_offline",
    "agent_heartbeat",
    "node_paired",
    "node_online",
    "node_offline",
    "node_heartbeat",
    "alert_created",
    "error",
    name="event_type",
)
EVENT_SEVERITY = sa.Enum(
    "info",
    "success",
    "warning",
    "error",
    "critical",
    name="event_severity",
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    EVENT_TYPE.create(bind, checkfirst=True)
    EVENT_SEVERITY.create(bind, checkfirst=True)

    op.create_table(
        "event_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", EVENT_TYPE, nullable=False),
        sa.Column(
            "severity",
            EVENT_SEVERITY,
            server_default="info",
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(length=255), nullable=True),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column(
            "is_heartbeat",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_logs_org_created_at", "event_logs", ["organization_id", "created_at"])
    op.create_index("ix_event_logs_created_at", "event_logs", ["created_at"])
    op.create_index("ix_event_logs_type_created_at", "event_logs", ["event_type", "created_at"])
    op.create_index(
        "ix_event_logs_heartbeat_created_at", "event_logs", ["is_heartbeat", "created_at"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_event_logs_heartbeat_created_at", table_name="event_logs")
    op.drop_index("ix_event_logs_type_created_at", table_name="event_logs")
    op.drop_index("ix_event_logs_created_at", table_name="event_logs")
    op.drop_index("ix_event_logs_org_created_at", table_name="event_logs")
    op.drop_table("event_logs")
    bind = op.get_bind()
    EVENT_SEVERITY.drop(bind, checkfirst=True)
    EVENT_TYPE.drop(bind, checkfirst=True)
