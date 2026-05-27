"""Feedback router — POST TP/FP marking."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import alert_repo, feedback_repo
from sentry_backend.schemas.feedback import FeedbackCreate, FeedbackPublic

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackPublic, status_code=status.HTTP_201_CREATED)
async def post_feedback(
    body: FeedbackCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> FeedbackPublic:
    alert = await alert_repo.get_alert_for_org(db, body.alert_id, org_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    fb = await feedback_repo.create_feedback(
        db,
        alert_id=body.alert_id,
        user_id=user.id,
        verdict=body.verdict,
        notes=body.notes,
    )
    return FeedbackPublic.model_validate(fb)
