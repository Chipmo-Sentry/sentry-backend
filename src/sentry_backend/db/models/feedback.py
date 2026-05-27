"""Feedback — staff TP/FP marking on an alert."""

import enum
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FeedbackVerdict(enum.StrEnum):
    true_positive = "true_positive"
    false_positive = "false_positive"
    unclear = "unclear"


class Feedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "feedback"

    alert_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        index=True,
        nullable=False,
    )
    verdict: Mapped[FeedbackVerdict] = mapped_column(
        Enum(FeedbackVerdict, name="feedback_verdict"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
