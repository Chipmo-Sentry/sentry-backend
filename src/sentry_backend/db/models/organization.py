"""Organization (multi-tenant root) + OrganizationMember junction."""
import enum
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(63), unique=True, index=True, nullable=False
    )


class OrgRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    staff = "staff"


class OrganizationMember(TimestampMixin, Base):
    __tablename__ = "organization_members"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[OrgRole] = mapped_column(
        Enum(OrgRole, name="org_role"), nullable=False
    )
