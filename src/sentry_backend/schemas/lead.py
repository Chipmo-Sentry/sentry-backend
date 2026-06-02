"""Lead schemas — public capture + admin triage."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from sentry_backend.db.models.lead import LeadStatus


class LeadCreate(BaseModel):
    """Public landing-page submission.

    `website` is a honeypot — a hidden field real users never fill. Bots that
    blindly fill every input trip it; the endpoint silently drops those.
    """

    email: EmailStr
    name: str | None = Field(default=None, max_length=255)
    organization: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    industry: str | None = Field(default=None, max_length=128)
    camera_count: int | None = Field(default=None, ge=0, le=100_000)
    source: str = Field(default="landing", max_length=64)
    website: str | None = Field(default=None, max_length=255)  # honeypot


class LeadPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str | None
    organization: str | None
    phone: str | None
    industry: str | None
    camera_count: int | None
    source: str
    status: LeadStatus
    notes: str | None
    created_at: datetime


class LeadUpdate(BaseModel):
    """Super-admin triage — change status and/or attach notes."""

    status: LeadStatus | None = None
    notes: str | None = Field(default=None, max_length=4000)
