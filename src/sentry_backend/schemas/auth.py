"""Auth request/response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=255)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    is_super_admin: bool
    is_active: bool
    created_at: datetime


class LoginResponse(BaseModel):
    # Tokens are delivered ONLY as httpOnly SameSite=Lax cookies (ADR-0017), never
    # in the body, so JavaScript/XSS cannot read them. The body carries the user.
    user: UserPublic
