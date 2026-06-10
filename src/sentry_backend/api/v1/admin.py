"""Super-admin only — manage orgs, users, memberships, dashboard stats."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.ai_node import AiNode
from sentry_backend.db.models.alert import Alert
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.feedback import Feedback
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.repository import ai_node_repo, lead_repo, org_repo, user_repo
from sentry_backend.schemas.admin import (
    AdminStats,
    OrgMemberPublic,
    UserAdminUpdate,
    would_self_lockout,
)
from sentry_backend.schemas.ai_node import (
    AiNodePairingCodePublic,
    AiNodePublic,
    AiNodeUpdate,
)
from sentry_backend.schemas.auth import UserPublic
from sentry_backend.schemas.lead import LeadPublic, LeadUpdate
from sentry_backend.schemas.org import (
    OrganizationCreate,
    OrganizationPublic,
    UserInvite,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/stats", response_model=AdminStats)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> AdminStats:
    async def _count(model: type) -> int:
        result = await db.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())

    async def _count_where(model: type, *where: Any) -> int:
        result = await db.execute(select(func.count()).select_from(model).where(*where))
        return int(result.scalar_one())

    now = datetime.now(UTC)
    online_cutoff = now - timedelta(minutes=3)
    day_ago = now - timedelta(hours=24)
    return AdminStats(
        orgs=await org_repo.count_orgs(db),
        users=await user_repo.count_users(db),
        stores=await _count(Store),
        cameras=await _count(Camera),
        alerts=await _count(Alert),
        cameras_enabled=await _count_where(Camera, Camera.enabled.is_(True)),
        ai_nodes=await _count_where(AiNode, AiNode.is_active.is_(True)),
        ai_nodes_online=await _count_where(
            AiNode, AiNode.is_active.is_(True), AiNode.last_seen_at >= online_cutoff
        ),
        alerts_24h=await _count_where(Alert, Alert.created_at >= day_ago),
    )


@router.get("/analytics/alerts")
async def alert_analytics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    range_: Annotated[str, Query(alias="range")] = "7d",
) -> dict[str, object]:
    """Alert breakdown for the observability dashboard (docs/19 Phase 2): how
    many suspicion alerts fired in `range`, split by VLM category, by review
    level, and by day — so you see WHAT the system is catching, over time."""
    span = _METRIC_SPANS.get(range_, _METRIC_SPANS["7d"])
    frm = datetime.now(UTC) - span

    async def _group(col: Any) -> dict[str, int]:
        rows = await db.execute(
            select(col, func.count()).where(Alert.created_at >= frm).group_by(col)
        )
        return {str(k): int(v) for k, v in rows.all()}

    by_category = await _group(Alert.category)
    by_level = await _group(Alert.alert_level)
    day_col = func.date_trunc("day", Alert.created_at).label("day")
    day_rows = await db.execute(
        select(day_col, func.count())
        .where(Alert.created_at >= frm)
        .group_by(day_col)
        .order_by(day_col)
    )
    by_day = [{"day": d.isoformat(), "count": int(c)} for d, c in day_rows.all()]
    return {
        "total": sum(by_category.values()),
        "by_category": by_category,
        "by_level": by_level,
        "by_day": by_day,
    }


@router.get("/analytics/feedback")
async def feedback_analytics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    range_: Annotated[str, Query(alias="range")] = "30d",
) -> dict[str, object]:
    """Close the feedback loop (docs/19 Phase 3). Joins staff verdicts (Feedback)
    to the alert's VLM category and reports, per category, how many were marked
    true_positive / false_positive / unclear + the false-alarm rate — plus
    read-only TUNING SUGGESTIONS for noisy categories (high FP rate). Applying a
    suggestion (weight/threshold change) stays a human action for now."""
    span = _METRIC_SPANS.get(range_, _METRIC_SPANS["30d"])
    frm = datetime.now(UTC) - span
    rows = await db.execute(
        select(Alert.category, Feedback.verdict, func.count())
        .join(Alert, Feedback.alert_id == Alert.id)
        .where(Feedback.created_at >= frm)
        .group_by(Alert.category, Feedback.verdict)
    )
    by_category: dict[str, dict[str, Any]] = {}
    totals = {"true_positive": 0, "false_positive": 0, "unclear": 0}
    for cat, verdict, n in rows.all():
        c = by_category.setdefault(
            str(cat), {"true_positive": 0, "false_positive": 0, "unclear": 0}
        )
        c[str(verdict)] = int(n)
        totals[str(verdict)] = totals.get(str(verdict), 0) + int(n)

    suggestions: list[dict[str, object]] = []
    for cat, c in by_category.items():
        total = c["true_positive"] + c["false_positive"] + c["unclear"]
        fp_rate = c["false_positive"] / total if total else 0.0
        c["total"] = total
        c["fp_rate"] = round(fp_rate, 2)
        # Enough signal + mostly false → suggest making this category stricter.
        if total >= 5 and fp_rate >= 0.5:
            suggestions.append(
                {
                    "category": cat,
                    "fp_rate": round(fp_rate, 2),
                    "samples": total,
                    "action": "raise_threshold",
                    "hint": (
                        f"'{cat}' сэжгийн {round(fp_rate * 100)}% нь худал сэрэлт "
                        f"({c['false_positive']}/{total}). Босго өсгөх / жин бууруулахыг бодолцоно уу."
                    ),
                }
            )
    return {
        "total": sum(totals.values()),
        "totals": totals,
        "by_category": by_category,
        "suggestions": suggestions,
    }


@router.get("/orgs", response_model=list[OrganizationPublic])
async def list_orgs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[OrganizationPublic]:
    orgs = await org_repo.list_orgs(db)
    return [OrganizationPublic.model_validate(o) for o in orgs]


@router.post(
    "/orgs",
    response_model=OrganizationPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_org(
    body: OrganizationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> OrganizationPublic:
    if await org_repo.get_org_by_slug(db, body.slug) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{body.slug}' богино нэр аль хэдийн ашиглагдсан байна.",
        )
    org = await org_repo.create_org(db, name=body.name, slug=body.slug)
    return OrganizationPublic.model_validate(org)


@router.get("/orgs/{org_id}", response_model=OrganizationPublic)
async def get_org(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> OrganizationPublic:
    org = await org_repo.get_org(db, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Байгууллага олдсонгүй.",
        )
    return OrganizationPublic.model_validate(org)


@router.get("/orgs/{org_id}/members", response_model=list[OrgMemberPublic])
async def list_org_members(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[OrgMemberPublic]:
    if await org_repo.get_org(db, org_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Байгууллага олдсонгүй.",
        )
    members = await org_repo.list_members(db, org_id)
    return [
        OrgMemberPublic(user=UserPublic.model_validate(user), role=role) for user, role in members
    ]


@router.get("/users", response_model=list[UserPublic])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[UserPublic]:
    users = await user_repo.list_users(db)
    return [UserPublic.model_validate(u) for u in users]


@router.post(
    "/users",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
)
async def invite_user(
    body: UserInvite,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> UserPublic:
    # Reject duplicate email
    if await user_repo.get_user_by_email(db, body.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ имэйл аль хэдийн бүртгэгдсэн байна.",
        )

    # Verify org exists
    org = await org_repo.get_org(db, body.organization_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Байгууллага олдсонгүй.",
        )

    user = await user_repo.create_user(
        db,
        email=body.email,
        password=body.password,
        is_super_admin=body.is_super_admin,
    )

    try:
        await org_repo.add_membership(
            db,
            user_id=user.id,
            organization_id=body.organization_id,
            role=body.role,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Хэрэглэгч энэ байгууллагын гишүүн аль хэдийн болсон байна.",
        ) from None

    return UserPublic.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserPublic)
async def update_user(
    user_id: UUID,
    body: UserAdminUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_super_admin)],
) -> UserPublic:
    if would_self_lockout(actor_id=str(actor.id), target_id=str(user_id), update=body):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Та өөрийнхөө super-admin эрхийг хасч болохгүй.",
        )

    user = await user_repo.get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Хэрэглэгч олдсонгүй.",
        )

    user = await user_repo.update_user_flags(
        db,
        user,
        is_active=body.is_active,
        is_super_admin=body.is_super_admin,
    )
    return UserPublic.model_validate(user)


@router.get("/leads", response_model=list[LeadPublic])
async def list_leads(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    limit: int = 200,
    offset: int = 0,
) -> list[LeadPublic]:
    leads = await lead_repo.list_leads(db, limit=min(limit, 500), offset=max(offset, 0))
    return [LeadPublic.model_validate(lead) for lead in leads]


@router.patch("/leads/{lead_id}", response_model=LeadPublic)
async def update_lead(
    lead_id: UUID,
    body: LeadUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> LeadPublic:
    lead = await lead_repo.get_lead(db, lead_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Хүсэлт олдсонгүй.")
    lead = await lead_repo.update_lead(db, lead, status=body.status, notes=body.notes)
    return LeadPublic.model_validate(lead)


# ── AI nodes (compute boxes running sentry-ai) ──────────────────────────
@router.post(
    "/ai-nodes/pairing-codes",
    response_model=AiNodePairingCodePublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_node_pairing_code(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_super_admin)],
) -> AiNodePairingCodePublic:
    code = await ai_node_repo.create_pairing_code(db, created_by_user_id=user.id)
    return AiNodePairingCodePublic(code=code.code, expires_at=code.expires_at)


@router.get("/ai-nodes", response_model=list[AiNodePublic])
async def list_ai_nodes(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[AiNodePublic]:
    nodes = await ai_node_repo.list_nodes(db)
    return [AiNodePublic.model_validate(n) for n in nodes]


_METRIC_SPANS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@router.get("/ai-nodes/{node_id}/metrics")
async def ai_node_metrics(
    node_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    range_: Annotated[str, Query(alias="range")] = "24h",
    bucket: str = "auto",
) -> list[dict[str, object]]:
    """Resource time-series (CPU/RAM/GPU) for the observability dashboard.

    `range` = 1h | 6h | 24h | 7d | 30d. `bucket` = auto | raw | hour (auto picks
    raw for ≤24h, hourly averages for wider ranges to keep the payload small).
    """
    span = _METRIC_SPANS.get(range_, _METRIC_SPANS["24h"])
    to = datetime.now(UTC)
    frm = to - span
    if bucket == "auto":
        bucket = "hour" if span > timedelta(hours=24) else "raw"
    return await ai_node_repo.get_metrics(db, node_id, frm=frm, to=to, bucket=bucket)


@router.post("/ai-nodes/{node_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_ai_node(
    node_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> None:
    node = await ai_node_repo.get_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI зангилаа олдсонгүй.")
    await ai_node_repo.deactivate_node(db, node)


@router.patch("/ai-nodes/{node_id}", response_model=AiNodePublic)
async def update_ai_node(
    node_id: UUID,
    body: AiNodeUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> AiNodePublic:
    node = await ai_node_repo.get_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI зангилаа олдсонгүй.")
    node = await ai_node_repo.update_node(
        db,
        node,
        name=body.name,
        enabled=body.enabled,
        provider=body.provider,
        frame_skip=body.frame_skip,
    )
    return AiNodePublic.model_validate(node)
