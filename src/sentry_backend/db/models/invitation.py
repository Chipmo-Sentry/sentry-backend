"""Org user invitation — a tokenized, emailed invite to join an organization.

An org owner/admin invites an email to their org with a role. We store only the
SHA-256 of the raw token (the raw token goes out in the emailed link). On accept,
the invitee sets their own password → a User + OrganizationMember are created.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sentry_backend.db.models.organization import OrgRole


class Invitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invitations_token_hash", "token_hash"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Reuse the existing org_role enum type (do NOT recreate it).
    role: Mapped[OrgRole] = mapped_column(
        PG_ENUM(OrgRole, name="org_role", create_type=False),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
