"""Store CRUD schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StoreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    timezone: str = Field(default="Asia/Ulaanbaatar", max_length=64)
    telegram_chat_id: str | None = Field(default=None, max_length=64)


class StoreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    timezone: str | None = Field(default=None, max_length=64)
    telegram_chat_id: str | None = Field(default=None, max_length=64)


class StorePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    name: str
    address: str | None
    timezone: str
    telegram_chat_id: str | None
    created_at: datetime
