"""Public lead capture — the landing-page demo-request form posts here.

No auth (it's a public marketing form). Defended by a honeypot field + a
per-IP rate limit. Super-admins triage captured leads via /api/v1/admin/leads.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.lead import LeadStatus
from sentry_backend.deps.db import get_db
from sentry_backend.logging_setup import get_logger
from sentry_backend.ratelimit import limiter
from sentry_backend.repository import lead_repo
from sentry_backend.schemas.lead import LeadCreate, LeadPublic
from sentry_backend.services import lead_notify
from sentry_backend.settings import get_settings

router = APIRouter(prefix="/api/v1/leads", tags=["leads"])

log = get_logger("sentry_backend.leads")


@router.post("", response_model=LeadPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(lambda: get_settings().lead_rate_limit)
async def submit_lead(
    request: Request,  # noqa: ARG001 — required by slowapi's limiter
    body: LeadCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadPublic:
    # Honeypot: real users never fill `website`. Bots that fill every field do.
    # Pretend success (echo the input, never persisted) so the bot gets no
    # signal that its submission was dropped.
    if body.website:
        log.info("lead_honeypot_drop", email=str(body.email))
        return LeadPublic(
            id=UUID(int=0),
            email=str(body.email),
            name=body.name,
            organization=body.organization,
            phone=body.phone,
            industry=body.industry,
            camera_count=body.camera_count,
            source=body.source,
            status=LeadStatus.new,
            notes=None,
            created_at=datetime.now(UTC),
        )

    lead = await lead_repo.create_lead(
        db,
        email=str(body.email),
        name=body.name,
        organization=body.organization,
        phone=body.phone,
        industry=body.industry,
        camera_count=body.camera_count,
        source=body.source,
    )
    await db.commit()
    await db.refresh(lead)
    await lead_notify.notify_new_lead(lead)
    return LeadPublic.model_validate(lead)
