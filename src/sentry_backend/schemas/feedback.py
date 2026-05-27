"""Feedback schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sentry_backend.db.models.feedback import FeedbackVerdict


class FeedbackCreate(BaseModel):
    alert_id: UUID
    verdict: FeedbackVerdict
    notes: str | None = Field(default=None, max_length=2000)


class FeedbackPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    alert_id: UUID
    user_id: UUID
    verdict: FeedbackVerdict
    notes: str | None
    created_at: datetime
