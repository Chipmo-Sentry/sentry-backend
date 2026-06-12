"""Super-admin billing — overview, topup, credit, promo codes, analytics."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.billing import (
    BillingJournal,
    BillingProfile,
    JournalKind,
    PromoCode,
    PromoKind,
)
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.organization import Organization
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.schemas.billing import (
    BillingAnalytics,
    BillingDayPoint,
    BillingOverview,
    CreditRequest,
    JournalEntryPublic,
    OrgBillingRow,
    PromoCodeCreate,
    PromoCodePublic,
    PromoCodeUpdate,
    TopupRequest,
)
from sentry_backend.services.billing import promo as promo_service
from sentry_backend.services.billing.ledger import get_or_create_profile, post_entry
from sentry_backend.services.billing.pricing import org_daily_charge_mnt
from sentry_backend.services.billing.status import BillingStatus, billing_status

router = APIRouter(prefix="/api/v1/admin/billing", tags=["admin-billing"])

_ANALYTIC_SPANS = {"7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}


async def _require_org(db: AsyncSession, org_id: UUID) -> Organization:
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Байгууллага олдсонгүй.")
    return org


@router.get("/overview", response_model=BillingOverview)
async def billing_overview(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> BillingOverview:
    now = datetime.now(UTC)

    # Per-store enabled-camera counts, grouped per org in Python (tier math
    # is per store, so we can't aggregate cameras straight to org level).
    store_rows = await db.execute(
        select(Store.organization_id, Store.id, func.count(Camera.id))
        .outerjoin(Camera, (Camera.store_id == Store.id) & Camera.enabled.is_(True))
        .group_by(Store.organization_id, Store.id)
    )
    counts_by_org: dict[UUID, list[int]] = {}
    for org_id, _store_id, n in store_rows.all():
        counts_by_org.setdefault(org_id, []).append(int(n))

    topup_rows = await db.execute(
        select(BillingJournal.org_id, func.max(BillingJournal.posted_at))
        .where(BillingJournal.kind == JournalKind.topup)
        .group_by(BillingJournal.org_id)
    )
    last_topups: dict[UUID, datetime] = dict(topup_rows.tuples().all())

    org_rows = await db.execute(
        select(Organization, BillingProfile)
        .outerjoin(BillingProfile, BillingProfile.org_id == Organization.id)
        .order_by(Organization.name)
    )
    rows: list[OrgBillingRow] = []
    for org, profile in org_rows.all():
        counts = counts_by_org.get(org.id, [])
        st = billing_status(
            balance_mnt=profile.balance_mnt if profile else 0,
            credit_until=profile.credit_until if profile else None,
            promo_free_until=profile.promo_free_until if profile else None,
            now=now,
        )
        rows.append(
            OrgBillingRow(
                org_id=org.id,
                name=org.name,
                slug=org.slug,
                balance_mnt=profile.balance_mnt if profile else 0,
                status=st,
                daily_rate_mnt=org_daily_charge_mnt(counts),
                stores_count=len(counts),
                cameras_count=sum(counts),
                credit_until=profile.credit_until if profile else None,
                promo_free_until=profile.promo_free_until if profile else None,
                last_topup_at=last_topups.get(org.id),
            )
        )
    return BillingOverview(
        orgs=rows,
        total_balance_mnt=sum(r.balance_mnt for r in rows),
        total_daily_rate_mnt=sum(r.daily_rate_mnt for r in rows),
        active_count=sum(1 for r in rows if r.status is BillingStatus.active),
        credit_count=sum(1 for r in rows if r.status is BillingStatus.credit),
        suspended_count=sum(1 for r in rows if r.status is BillingStatus.suspended),
    )


@router.get("/orgs/{org_id}/journal", response_model=list[JournalEntryPublic])
async def org_journal(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JournalEntryPublic]:
    await _require_org(db, org_id)
    rows = await db.execute(
        select(BillingJournal)
        .where(BillingJournal.org_id == org_id)
        .order_by(BillingJournal.posted_at.desc(), BillingJournal.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [JournalEntryPublic.model_validate(e) for e in rows.scalars().all()]


@router.post("/orgs/{org_id}/topup", response_model=JournalEntryPublic)
async def topup(
    org_id: UUID,
    body: TopupRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_super_admin)],
) -> JournalEntryPublic:
    """Record a received payment: Dr cash / Cr org_wallet, balance += amount."""
    await _require_org(db, org_id)
    entry = await post_entry(
        db,
        org_id=org_id,
        kind=JournalKind.topup,
        amount_mnt=body.amount_mnt,
        description=body.note or "Цэнэглэлт",
        created_by_user_id=actor.id,
    )
    return JournalEntryPublic.model_validate(entry)


@router.post("/orgs/{org_id}/credit", status_code=status.HTTP_204_NO_CONTENT)
async def grant_credit(
    org_id: UUID,
    body: CreditRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> None:
    """Emergency unlock: keep a non-paying org alive until `until`."""
    await _require_org(db, org_id)
    if body.until <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`until` нь ирээдүйн цаг байх ёстой.",
        )
    profile = await get_or_create_profile(db, org_id)
    profile.credit_until = body.until
    profile.credit_note = body.note


@router.delete("/orgs/{org_id}/credit", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_credit(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> None:
    await _require_org(db, org_id)
    profile = await get_or_create_profile(db, org_id)
    profile.credit_until = None
    profile.credit_note = None


@router.get("/promo-codes", response_model=list[PromoCodePublic])
async def list_promo_codes(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[PromoCodePublic]:
    rows = await db.execute(select(PromoCode).order_by(PromoCode.created_at.desc()))
    return [PromoCodePublic.model_validate(p) for p in rows.scalars().all()]


@router.post("/promo-codes", response_model=PromoCodePublic, status_code=status.HTTP_201_CREATED)
async def create_promo_code(
    body: PromoCodeCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_super_admin)],
) -> PromoCodePublic:
    if body.kind is PromoKind.bonus_amount and not body.amount_mnt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bonus_amount кодод amount_mnt шаардлагатай.",
        )
    if body.kind is PromoKind.free_days and not body.free_days:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="free_days кодод free_days шаардлагатай.",
        )
    promo = PromoCode(
        code=body.code or promo_service.generate_code(),
        kind=body.kind,
        amount_mnt=body.amount_mnt if body.kind is PromoKind.bonus_amount else None,
        free_days=body.free_days if body.kind is PromoKind.free_days else None,
        valid_until=body.valid_until,
        max_redemptions=body.max_redemptions,
        note=body.note,
        created_by_user_id=actor.id,
    )
    db.add(promo)
    try:
        await db.flush()
    except IntegrityError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ код аль хэдийн бүртгэлтэй байна.",
        ) from e
    return PromoCodePublic.model_validate(promo)


@router.patch("/promo-codes/{promo_id}", response_model=PromoCodePublic)
async def update_promo_code(
    promo_id: UUID,
    body: PromoCodeUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> PromoCodePublic:
    promo = await db.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Код олдсонгүй.")
    if body.active is not None:
        promo.active = body.active
    if body.note is not None:
        promo.note = body.note
    await db.flush()
    return PromoCodePublic.model_validate(promo)


@router.get("/analytics", response_model=BillingAnalytics)
async def billing_analytics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    range_: Annotated[str, Query(alias="range")] = "30d",
) -> BillingAnalytics:
    """Daily money flow (usage / topup / promo) for the superadmin chart."""
    now = datetime.now(UTC)
    frm = now - _ANALYTIC_SPANS.get(range_, _ANALYTIC_SPANS["30d"])

    day_col = func.date_trunc("day", BillingJournal.posted_at).label("day")
    rows = await db.execute(
        select(day_col, BillingJournal.kind, func.sum(BillingJournal.amount_mnt))
        .where(BillingJournal.posted_at >= frm)
        .group_by(day_col, BillingJournal.kind)
        .order_by(day_col)
    )
    by_day: dict[str, dict[str, int]] = {}
    for day, kind, total in rows.all():
        key = day.date().isoformat()
        bucket = by_day.setdefault(key, {"usage_mnt": 0, "topup_mnt": 0, "promo_mnt": 0})
        if kind is JournalKind.usage_charge:
            bucket["usage_mnt"] += int(total)
        elif kind is JournalKind.topup:
            bucket["topup_mnt"] += int(total)
        elif kind is JournalKind.promo_credit:
            bucket["promo_mnt"] += int(total)

    points = [BillingDayPoint(day=d, **vals) for d, vals in sorted(by_day.items())]
    totals = BillingDayPoint(
        day=range_,
        usage_mnt=sum(p.usage_mnt for p in points),
        topup_mnt=sum(p.topup_mnt for p in points),
        promo_mnt=sum(p.promo_mnt for p in points),
    )

    # Status counts over all orgs (missing profile = empty wallet).
    org_rows = await db.execute(
        select(BillingProfile)
        .select_from(Organization)
        .outerjoin(BillingProfile, BillingProfile.org_id == Organization.id)
    )
    suspended = 0
    credit = 0
    for (profile,) in org_rows.all():
        st = billing_status(
            balance_mnt=profile.balance_mnt if profile else 0,
            credit_until=profile.credit_until if profile else None,
            promo_free_until=profile.promo_free_until if profile else None,
            now=now,
        )
        if st is BillingStatus.suspended:
            suspended += 1
        elif st is BillingStatus.credit:
            credit += 1
    return BillingAnalytics(
        by_day=points, totals=totals, suspended_count=suspended, credit_count=credit
    )
