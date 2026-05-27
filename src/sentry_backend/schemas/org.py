"""Organization + invite schemas (super-admin scope)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from sentry_backend.db.models.organization import OrgRole


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")


class OrganizationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    created_at: datetime


class UserInvite(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=255)
    organization_id: UUID
    role: OrgRole
    is_super_admin: bool = False
