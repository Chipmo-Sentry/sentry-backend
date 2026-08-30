"""Super-admin only — manage orgs, users, memberships, dashboard stats."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent
from sentry_backend.db.models.ai_node import AiNode
from sentry_backend.db.models.alert import Alert
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.event_log import EventSeverity, EventType
from sentry_backend.db.models.feedback import Feedback
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.repository import (
    ai_node_repo,
    alert_repo,
    camera_repo,
    edge_config_repo,
    feedback_repo,
    lead_repo,
    org_repo,
    store_repo,
    telegram_config_repo,
    user_repo,
)
from sentry_backend.schemas.admin import (
    AdminAlertRow,
    AdminStats,
    OrgMemberPublic,
    StoreAdminRow,
    StoreAdminUpdate,
    TelegramConfigUpdate,
    TelegramConfigView,
    UserAdminRow,
    UserAdminUpdate,
    UserMembershipRow,
    would_self_lockout,
)
from sentry_backend.schemas.ai_node import (
    AiNodePairingCodePublic,
    AiNodePublic,
    AiNodeUpdate,
)
from sentry_backend.schemas.alert import AlertPublic
from sentry_backend.schemas.auth import UserPublic
from sentry_backend.schemas.edge import (
    EdgeConfigAdminView,
    EdgeConfigOverridesIn,
    merged_edge_payload,
)
from sentry_backend.schemas.lead import LeadPublic, LeadUpdate
from sentry_backend.schemas.org import (
    OrganizationCreate,
    OrganizationPublic,
    UserInvite,
)
from sentry_backend.services import event_log, org_delete

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


@router.get("/alerts", response_model=list[AdminAlertRow])
async def list_alerts_admin(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminAlertRow]:
    """Recent alerts across ALL orgs for the superadmin pipeline ("Урсгал") page.

    Each row is one problematic clip's end-to-end journey — camera → behaviours
    (triggered_*) → VLM (reasoning/confidence/model) → decision (alert_level/
    category) → review (feedback_verdict) — enriched with org/store/camera display
    names. Newest first. Filtering is done client-side over the fetched window."""
    rows = await alert_repo.list_recent_admin(db, limit=limit, offset=offset)
    alerts = [row[0] for row in rows]
    verdicts = await feedback_repo.latest_verdicts_for_alerts(db, [a.id for a in alerts])
    out: list[AdminAlertRow] = []
    for alert, org_name, store_name, cam_name in rows:
        base = AlertPublic.model_validate(alert)
        base.feedback_verdict = verdicts.get(alert.id)
        out.append(
            AdminAlertRow(
                **base.model_dump(),
                organization_name=org_name,
                store_name=store_name,
                camera_name=cam_name,
            )
        )
    return out


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


# Confidence calibration buckets: (low, high, label). The high edge of the last
# bucket is > 1.0 so a perfect 1.0 confidence still lands somewhere.
_CONF_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.30, "0.00–0.30"),
    (0.30, 0.50, "0.30–0.50"),
    (0.50, 0.70, "0.50–0.70"),
    (0.70, 0.85, "0.70–0.85"),
    (0.85, 1.0001, "0.85–1.00"),
)
# Alert levels actually shown to staff — false ones here drive alert fatigue.
_STAFF_LEVELS = {"notify", "review"}


def _precision(tp: int, fp: int) -> float | None:
    """TP / (TP + FP), or None when there's nothing labelled to divide by."""
    return round(tp / (tp + fp), 3) if (tp + fp) else None


def compute_quality_metrics(
    range_: str,
    total_alerts: int,
    feedback_rows: list[tuple[Any, str, float | None, str, str, datetime]],
    days: float,
) -> dict[str, object]:
    """Pure detection-quality aggregation (no DB) so it's unit-testable.

    `feedback_rows` = (alert_id, category, confidence, alert_level, verdict,
    feedback_ts) for every feedback on alerts in the window. Collapses to the
    LATEST verdict per alert, then derives precision (overall + per category),
    labelled coverage, confidence calibration, and false-alerts-per-day."""
    latest: dict[Any, dict[str, Any]] = {}
    for aid, cat, conf, level, verdict, fts in feedback_rows:
        prev = latest.get(aid)
        if prev is None or fts > prev["fts"]:
            latest[aid] = {
                "cat": str(cat),
                "conf": float(conf) if conf is not None else 0.0,
                "level": str(level),
                "verdict": str(verdict),
                "fts": fts,
            }

    tp = fp = unclear = 0
    by_cat: dict[str, dict[str, int]] = {}
    buckets = {b[2]: {"tp": 0, "fp": 0} for b in _CONF_BUCKETS}
    false_staff_alerts = 0
    for r in latest.values():
        v, cat, conf, level = r["verdict"], r["cat"], r["conf"], r["level"]
        c = by_cat.setdefault(cat, {"true_positive": 0, "false_positive": 0, "unclear": 0})
        c[v] = c.get(v, 0) + 1
        if v == "true_positive":
            tp += 1
        elif v == "false_positive":
            fp += 1
            if level in _STAFF_LEVELS:
                false_staff_alerts += 1
        else:
            unclear += 1
        if v in ("true_positive", "false_positive"):
            for low, high, label in _CONF_BUCKETS:
                if low <= conf < high:
                    buckets[label]["tp" if v == "true_positive" else "fp"] += 1
                    break

    return {
        "range": range_,
        "total_alerts": total_alerts,
        "labeled": len(latest),
        "coverage": round(len(latest) / total_alerts, 3) if total_alerts else 0.0,
        "tp": tp,
        "fp": fp,
        "unclear": unclear,
        "precision": _precision(tp, fp),
        "by_category": [
            {
                "category": cat,
                "tp": c["true_positive"],
                "fp": c["false_positive"],
                "unclear": c["unclear"],
                "precision": _precision(c["true_positive"], c["false_positive"]),
            }
            for cat, c in sorted(by_cat.items())
        ],
        "by_confidence": [
            {
                "bucket": label,
                "tp": buckets[label]["tp"],
                "fp": buckets[label]["fp"],
                "tp_rate": _precision(buckets[label]["tp"], buckets[label]["fp"]),
            }
            for _, _, label in _CONF_BUCKETS
        ],
        "false_alerts_per_day": round(false_staff_alerts / max(1.0, days), 2),
    }


@router.get("/analytics/quality")
async def quality_analytics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    range_: Annotated[str, Query(alias="range")] = "30d",
) -> dict[str, object]:
    """Detection-QUALITY metrics derived from staff feedback — answers «how
    ACCURATE are we?», not just «how many alerts fired». Read-only.

    - precision = TP / (TP + FP) over labelled alerts (unclear excluded), overall
      and per VLM category.
    - coverage = labelled alerts / all alerts in range — precision is only as
      trustworthy as how much got reviewed, so we surface it explicitly.
    - confidence calibration: TP-rate per confidence bucket — validates whether
      the alert-level thresholds (0.30 / 0.70 / 0.85) actually track correctness.
    - false_alerts_per_day = FP-labelled notify/review alerts ÷ days — the
      alert-fatigue number (the #1 reason staff stop trusting the system).

    Uses the LATEST verdict per alert (an alert can be reviewed more than once),
    scoped by alert creation time so it reflects the model's recent behaviour."""
    span = _METRIC_SPANS.get(range_, _METRIC_SPANS["30d"])
    frm = datetime.now(UTC) - span
    days = span.total_seconds() / 86400.0

    total_alerts = int(
        (
            await db.execute(select(func.count()).select_from(Alert).where(Alert.created_at >= frm))
        ).scalar()
        or 0
    )
    rows = await db.execute(
        select(
            Alert.id,
            Alert.category,
            Alert.confidence,
            Alert.alert_level,
            Feedback.verdict,
            Feedback.created_at,
        )
        .join(Feedback, Feedback.alert_id == Alert.id)
        .where(Alert.created_at >= frm)
    )
    feedback_rows = [
        (aid, str(cat), conf, str(level), str(verdict), fts)
        for aid, cat, conf, level, verdict, fts in rows.all()
    ]
    return compute_quality_metrics(range_, total_alerts, feedback_rows, days)


# Staff verdict → eval ground-truth label. unclear is dropped (not a label).
_VERDICT_TO_LABEL = {"true_positive": "theft", "false_positive": "benign"}


def build_eval_dataset(
    rows: list[tuple[Any, Any, str, float | None, str, datetime]],
) -> list[dict[str, object]]:
    """Turn feedback'd alerts into eval-manifest entries (score mode): latest
    verdict per alert → ground-truth label, VLM category → prediction. `unclear`
    verdicts are excluded. Pure (no DB) so it's unit-testable.

    rows = (alert_id, clip_id, category, confidence, verdict, feedback_ts)."""
    latest: dict[Any, dict[str, Any]] = {}
    for aid, clip_id, cat, conf, verdict, fts in rows:
        prev = latest.get(aid)
        if prev is None or fts > prev["fts"]:
            latest[aid] = {
                "clip_id": clip_id,
                "cat": str(cat),
                "conf": conf,
                "verdict": str(verdict),
                "fts": fts,
            }
    entries: list[dict[str, object]] = []
    for r in latest.values():
        label = _VERDICT_TO_LABEL.get(r["verdict"])
        if label is None:  # unclear → not a usable ground-truth label
            continue
        entries.append(
            {
                "path": str(r["clip_id"]),
                "label": label,
                "predicted": r["cat"],
                "confidence": (float(r["conf"]) if r["conf"] is not None else None),
            }
        )
    return entries


@router.get("/eval/dataset")
async def eval_dataset(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    range_: Annotated[str, Query(alias="range")] = "30d",
) -> dict[str, object]:
    """Export feedback-labelled alerts as an eval manifest (score mode) for the
    sentry-ai harness: `python -m sentry_ai.eval score dataset.json`. Staff verdict
    is the ground-truth label (true_positive→theft, false_positive→benign, unclear
    dropped); the VLM category is the prediction — so it scores PRECISION on real
    production data with no model run. (Recall needs a curated clip set — see the
    harness README.) Latest verdict per alert."""
    span = _METRIC_SPANS.get(range_, _METRIC_SPANS["30d"])
    frm = datetime.now(UTC) - span
    rows = await db.execute(
        select(
            Alert.id,
            Alert.clip_id,
            Alert.category,
            Alert.confidence,
            Feedback.verdict,
            Feedback.created_at,
        )
        .join(Feedback, Feedback.alert_id == Alert.id)
        .where(Alert.created_at >= frm)
    )
    dataset_rows = [
        (aid, clip_id, str(cat), conf, str(verdict), fts)
        for aid, clip_id, cat, conf, verdict, fts in rows.all()
    ]
    clips = build_eval_dataset(dataset_rows)
    return {"range": range_, "count": len(clips), "clips": clips}


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
    actor: Annotated[User, Depends(require_super_admin)],
) -> OrganizationPublic:
    if await org_repo.get_org_by_slug(db, body.slug) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{body.slug}' богино нэр аль хэдийн ашиглагдсан байна.",
        )
    org = await org_repo.create_org(db, name=body.name, slug=body.slug)
    await event_log.emit(
        db,
        event_type=EventType.org_created,
        severity=EventSeverity.success,
        message=f"Байгууллага үүслээ: {org.name}",
        organization_id=org.id,
        actor_user_id=actor.id,
        actor_label=actor.email,
        detail={"slug": org.slug},
    )
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


@router.delete("/orgs/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> None:
    """Delete a tenant entirely (T15 #3 — the privacy policy's deletion promise).

    Every org-scoped row cascades in the DB; the org's clip evidence FILES are
    unlinked from disk first (a cascade can't reach the filesystem)."""
    org = await org_repo.get_org(db, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Байгууллага олдсонгүй.",
        )
    await org_delete.delete_organization(db, org)


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


@router.get("/users", response_model=list[UserAdminRow])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[UserAdminRow]:
    users = await user_repo.list_users(db)
    # One JOIN for every membership, then group by user (avoids an N+1 per user).
    memberships: dict[UUID, list[UserMembershipRow]] = {}
    for user_id, org_id, org_name, role in await org_repo.list_all_memberships(db):
        memberships.setdefault(user_id, []).append(
            UserMembershipRow(
                organization_id=str(org_id),
                organization_name=org_name,
                role=role,
            )
        )
    return [
        UserAdminRow(
            **UserPublic.model_validate(u).model_dump(),
            memberships=memberships.get(u.id, []),
        )
        for u in users
    ]


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


@router.delete("/ai-nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ai_node(
    node_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> None:
    """Hard-delete a node — removes it from the superadmin + org node lists.
    Revoke only deactivates (row stays); this removes the row entirely for a
    decommissioned node. Metrics cascade; pairing codes/event-log survive."""
    node = await ai_node_repo.get_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI зангилаа олдсонгүй.")
    await ai_node_repo.delete_node(db, node)


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
        breach_mode=body.breach_mode,
        person_conf=body.person_conf,
        item_conf=body.item_conf,
        item_every_n=body.item_every_n,
        scan_interval_sec=body.scan_interval_sec,
        frames_per_clip=body.frames_per_clip,
        frame_max_dim=body.frame_max_dim,
        staff_badge_color=body.staff_badge_color,
    )
    return AiNodePublic.model_validate(node)


# ── Per-store edge config (ADR-0029 I3 / I9) ────────────────────────────────


@router.get("/stores", response_model=list[StoreAdminRow])
async def list_stores_admin(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[StoreAdminRow]:
    """Every store across ALL orgs with its org name + camera count — drives the
    superadmin store pickers (e.g. the per-store edge-config editor)."""
    rows = await store_repo.list_all_with_org_and_camera_count(db)
    return [
        StoreAdminRow(
            id=str(store.id),
            name=store.name,
            organization_id=str(store.organization_id),
            organization_name=org_name,
            camera_count=cam_n,
            agent_stream_push_url=store.agent_stream_push_url,
            agent_tunnel_hostname=store.agent_tunnel_hostname,
            agent_tunnel_token_set=bool(store.agent_tunnel_token),
        )
        for store, org_name, cam_n in rows
    ]


@router.patch("/stores/{store_id}", response_model=StoreAdminRow)
async def update_store_admin(
    store_id: UUID,
    body: StoreAdminUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> StoreAdminRow:
    """Superadmin edit of a store's cloud push target. Repoints where the store's
    agent pushes (e.g. after a vast.ai instance restart changes the IP/port) with
    no backend redeploy. Empty string clears it back to the global env URL."""
    store = await store_repo.get_store_any_org(db, store_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    await store_repo.update_store(
        db,
        store,
        agent_stream_push_url=body.agent_stream_push_url,
        agent_tunnel_token=body.agent_tunnel_token,
        agent_tunnel_hostname=body.agent_tunnel_hostname,
    )
    cam_n = await camera_repo.count_cameras_for_store(db, store.id)
    org = await org_repo.get_org(db, store.organization_id)
    return StoreAdminRow(
        id=str(store.id),
        name=store.name,
        organization_id=str(store.organization_id),
        organization_name=org.name if org else "",
        camera_count=cam_n,
        agent_stream_push_url=store.agent_stream_push_url,
        agent_tunnel_hostname=store.agent_tunnel_hostname,
        agent_tunnel_token_set=bool(store.agent_tunnel_token),
    )


@router.get("/edge-config", response_model=EdgeConfigAdminView)
async def get_global_edge_config(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> EdgeConfigAdminView:
    """The ONE global edge tunable overrides + version + the effective merged
    config every store's agents receive (ADR-0029 I3; global per founder request)."""
    row = await edge_config_repo.get_global_row(db)
    version, overrides = edge_config_repo.parse_row(row)
    return EdgeConfigAdminView(
        store_id="global",
        version=version,
        overrides=overrides,
        updated_at=row.updated_at_db if row else None,
        effective=merged_edge_payload(version, overrides),
    )


@router.put("/edge-config", response_model=EdgeConfigAdminView)
async def set_global_edge_config(
    body: EdgeConfigOverridesIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> EdgeConfigAdminView:
    """Set the global edge tunable overrides (partial); bumps `version` so ALL
    store agents re-apply. An empty body resets to the agent defaults (the version
    still bumps, so the agents pick up the reset)."""
    overrides = body.model_dump(exclude_none=True)
    row = await edge_config_repo.set_global(db, overrides)
    await db.commit()
    await db.refresh(row)  # load server-side updated_at
    version, ov = edge_config_repo.parse_row(row)
    return EdgeConfigAdminView(
        store_id="global",
        version=version,
        overrides=ov,
        updated_at=row.updated_at_db,
        effective=merged_edge_payload(version, ov),
    )


@router.get("/telegram-config", response_model=TelegramConfigView)
async def get_telegram_config(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> TelegramConfigView:
    """Whether a platform Telegram bot token is set (+ a last-4 hint). The full
    secret is never returned."""
    configured, hint = await telegram_config_repo.get_status(db)
    return TelegramConfigView(configured=configured, token_hint=hint)


@router.put("/telegram-config", response_model=TelegramConfigView)
async def set_telegram_config(
    body: TelegramConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> TelegramConfigView:
    """Set (non-empty) or clear (empty string) the platform Telegram bot token.
    Encrypted at rest; wins over the env fallback. Per-store chat ids stay on
    each store (frontend «Дэлгүүр засах»)."""
    await telegram_config_repo.set_token(db, body.bot_token.strip() or None)
    await db.commit()
    configured, hint = await telegram_config_repo.get_status(db)
    return TelegramConfigView(configured=configured, token_hint=hint)


# Cameras whose store agent was seen within this window count as reporting; a
# stale agent means we can't trust its per-camera push flags, so its cameras
# are "unknown", not falsely "offline".
_AGENT_ONLINE_WINDOW = timedelta(minutes=3)


@router.get("/analytics/system-health")
async def system_health(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    range_: Annotated[str, Query(alias="range")] = "30d",
) -> dict[str, object]:
    """Model-performance / operations snapshot for the superadmin dashboard.

    - cameras: live availability (streaming now / enabled) + the offline list
      with the real reason (agent push last_error). Historical uptime % needs a
      heartbeat time-series we don't store yet, so this is a CURRENT-state
      availability number, labelled as such in the UI.
    - response_time: alert → first-human-action latency, proxied by the gap
      between an alert and its first staff feedback (we have no separate
      acknowledge action yet). Median is the headline; mean + count for context.

    Precision / false-positive rate live on /analytics/quality (feedback-based);
    the dashboard shows both together. Recall isn't derivable — a missed theft
    leaves no record to count — so it's deliberately absent.
    """
    now = datetime.now(UTC)

    # ── Cameras: match each enabled camera to its store agent's push state ──
    cam_rows = (
        await db.execute(
            select(Camera.mediamtx_path, Camera.name, Store.name, Camera.store_id)
            .join(Store, Camera.store_id == Store.id)
            .where(Camera.enabled.is_(True))
        )
    ).all()
    agents = (await db.execute(select(Agent).where(Agent.is_active.is_(True)))).scalars().all()
    push_by_path: dict[str, dict[str, Any]] = {}
    for a in agents:
        fresh = a.last_seen_at is not None and now - a.last_seen_at < _AGENT_ONLINE_WINDOW
        for entry in a.push_status or []:
            p = entry.get("path")
            if isinstance(p, str) and p:
                push_by_path[p] = {
                    "running": bool(entry.get("running")),
                    "fresh": fresh,
                    "last_error": entry.get("last_error"),
                }

    online = offline = unknown = 0
    offline_list: list[dict[str, object]] = []
    for path, cam_name, store_name, _sid in cam_rows:
        st = push_by_path.get(path) if path else None
        if st is None:
            unknown += 1
            continue
        if st["fresh"] and st["running"]:
            online += 1
        else:
            offline += 1
            offline_list.append(
                {
                    "camera": cam_name,
                    "store": store_name,
                    "reason": (
                        "Агент офлайн" if not st["fresh"] else (st["last_error"] or "Дамжуулал зогссон")
                    ),
                }
            )
    total_enabled = len(cam_rows)
    measurable = online + offline  # exclude unknown from the availability ratio
    cameras = {
        "total_enabled": total_enabled,
        "online": online,
        "offline": offline,
        "unknown": unknown,
        "availability_pct": round(100.0 * online / measurable, 1) if measurable else None,
        "offline_list": offline_list[:50],
    }

    # ── Alert → response time (feedback-as-acknowledge proxy) ──
    span = _METRIC_SPANS.get(range_, _METRIC_SPANS["30d"])
    frm = now - span
    delta_rows = (
        await db.execute(
            select(Alert.created_at, func.min(Feedback.created_at))
            .join(Feedback, Feedback.alert_id == Alert.id)
            .where(Alert.created_at >= frm)
            .group_by(Alert.id, Alert.created_at)
        )
    ).all()
    mins = sorted(
        (fb - created).total_seconds() / 60.0
        for created, fb in delta_rows
        if fb is not None and fb >= created
    )
    if mins:
        n = len(mins)
        median = mins[n // 2] if n % 2 else (mins[n // 2 - 1] + mins[n // 2]) / 2.0
        response_time = {
            "count": n,
            "median_min": round(median, 1),
            "mean_min": round(sum(mins) / n, 1),
        }
    else:
        response_time = {"count": 0, "median_min": None, "mean_min": None}

    return {"range": range_, "cameras": cameras, "response_time": response_time}
