"""Customer billing — wallet summary, journal history, promo redemption."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.billing import BillingJournal, PromoKind
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import (
    get_current_organization_id,
    get_current_organization_id_admin,
)
from sentry_backend.schemas.billing import (
    BillingSummary,
    JournalEntryPublic,
    PromoRedeemRequest,
    PromoRedeemResponse,
    StoreBillingLine,
)
from sentry_backend.services.billing import promo as promo_service
from sentry_backend.services.billing.ledger import get_or_create_profile
from sentry_backend.services.billing.pricing import (
    daily_from_monthly,
    store_monthly_mnt,
    tier_for,
)
from sentry_backend.services.billing.status import billing_status

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


async def _store_lines(db: AsyncSession, org_id: UUID) -> list[StoreBillingLine]:
    rows = await db.execute(
        select(Store.id, Store.name, func.count(Camera.id))
        .outerjoin(Camera, (Camera.store_id == Store.id) & Camera.enabled.is_(True))
        .where(Store.organization_id == org_id)
        .group_by(Store.id, Store.name)
        .order_by(Store.name)
    )
    lines: list[StoreBillingLine] = []
    for store_id, name, cam_count in rows.all():
        n = int(cam_count)
        tier = tier_for(n) if n > 0 else None
        lines.append(
            StoreBillingLine(
                store_id=store_id,
                name=name,
                active_cameras=n,
                tier=tier.name if tier else None,
                platform_fee_mnt=tier.platform_fee_mnt if tier else 0,
                camera_fee_mnt=tier.camera_fee_mnt if tier else 0,
                monthly_mnt=store_monthly_mnt(n),
            )
        )
    return lines


@router.get("", response_model=BillingSummary)
async def get_billing(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> BillingSummary:
    """Wallet balance + status + the per-store rate breakdown."""
    now = datetime.now(UTC)
    profile = await get_or_create_profile(db, org_id)
    stores = await _store_lines(db, org_id)
    monthly = sum(line.monthly_mnt for line in stores)
    daily = sum(daily_from_monthly(line.monthly_mnt) for line in stores)
    return BillingSummary(
        balance_mnt=profile.balance_mnt,
        status=billing_status(
            balance_mnt=profile.balance_mnt,
            credit_until=profile.credit_until,
            promo_free_until=profile.promo_free_until,
            now=now,
        ),
        credit_until=profile.credit_until,
        promo_free_until=profile.promo_free_until,
        daily_rate_mnt=daily,
        monthly_rate_mnt=monthly,
        stores=stores,
    )


@router.get("/journal", response_model=list[JournalEntryPublic])
async def list_journal(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JournalEntryPublic]:
    """Ledger entries for the org, newest first."""
    rows = await db.execute(
        select(BillingJournal)
        .where(BillingJournal.org_id == org_id)
        .order_by(BillingJournal.posted_at.desc(), BillingJournal.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [JournalEntryPublic.model_validate(e) for e in rows.scalars().all()]


@router.post("/promo", response_model=PromoRedeemResponse)
async def redeem_promo(
    body: PromoRedeemRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
    user: Annotated[User, Depends(get_current_user)],
) -> PromoRedeemResponse:
    """Redeem a promo code (owner/admin only)."""
    now = datetime.now(UTC)
    try:
        error, promo = await promo_service.redeem(
            db, org_id=org_id, code=body.code, user_id=user.id, now=now
        )
    except IntegrityError as e:
        # Race on the unique (promo_id, org_id) index — same as already-redeemed.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=promo_service.ERR_ALREADY_REDEEMED,
        ) from e
    if error is not None or promo is None:
        code_map = {
            promo_service.ERR_NOT_FOUND: status.HTTP_404_NOT_FOUND,
            promo_service.ERR_ALREADY_REDEEMED: status.HTTP_409_CONFLICT,
            promo_service.ERR_EXHAUSTED: status.HTTP_410_GONE,
        }
        raise HTTPException(
            status_code=code_map.get(error or "", status.HTTP_400_BAD_REQUEST),
            detail=error or promo_service.ERR_NOT_FOUND,
        )
    profile = await get_or_create_profile(db, org_id)
    return PromoRedeemResponse(
        code=promo.code,
        kind=promo.kind,
        amount_mnt=promo.amount_mnt if promo.kind is PromoKind.bonus_amount else None,
        free_days=promo.free_days if promo.kind is PromoKind.free_days else None,
        balance_mnt=profile.balance_mnt,
        promo_free_until=profile.promo_free_until,
    )
