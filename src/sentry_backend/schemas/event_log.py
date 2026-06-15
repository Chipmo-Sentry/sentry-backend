"""EventLog schemas — activity/audit timeline wire format."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sentry_backend.db.models.event_log import EventSeverity, EventType


class EventLogPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime  # timestamptz, microsecond precision → UI renders HH:MM:SS.mmm
    organization_id: UUID | None
    event_type: EventType
    severity: EventSeverity
    message: str
    actor_user_id: UUID | None = None
    actor_label: str | None = None
    store_id: UUID | None = None
    camera_id: UUID | None = None
    agent_id: UUID | None = None
    ai_node_id: UUID | None = None
    detail: dict[str, Any] | None = None
    is_heartbeat: bool = False
