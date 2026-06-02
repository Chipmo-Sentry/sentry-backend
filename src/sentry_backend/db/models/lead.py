"""Lead — a demo/pilot request captured from the public landing page.

Pre-customer: no FK to organizations/users. The landing site POSTs to the
public `/api/v1/leads` endpoint; super-admins triage these in the admin panel.
"""

import enum

from sqlalchemy import Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LeadStatus(enum.StrEnum):
    new = "new"
    contacted = "contacted"
    qualified = "qualified"
    closed = "closed"


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leads"

    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    camera_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="landing", nullable=False)
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, name="lead_status"),
        default=LeadStatus.new,
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
