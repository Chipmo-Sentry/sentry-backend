"""Feedback router — POST TP/FP marking."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import alert_repo, feedback_repo, rag_case_repo
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сэрэмжлүүлэг олдсонгүй.")
    # One verdict per (alert, user): re-submitting updates the existing row
    # instead of stacking duplicates, so the UI shows a single settled answer.
    fb, changed = await feedback_repo.upsert_feedback(
        db,
        alert_id=body.alert_id,
        user_id=user.id,
        verdict=body.verdict,
        notes=body.notes,
    )
    # Close the RAG loop (docs/19 Phase 4): a staff-verified alert whose reasoning
    # the AI node already embedded becomes a retrievable case for this store — so
    # future verifications get it as few-shot context. No re-embed needed. Only on
    # a real new/changed decision — an idempotent re-submit must not spawn a dupe.
    if changed and alert.embedding and alert.reasoning:
        await rag_case_repo.add_case(
            db,
            store_id=alert.store_id,
            verdict=str(body.verdict),
            category=str(alert.category) if alert.category else None,
            description=alert.reasoning,
            embedding=list(alert.embedding),
        )
    return FeedbackPublic.model_validate(fb)
