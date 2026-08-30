"""Stores router — org-scoped CRUD."""

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent
from sentry_backend.db.models.alert import Alert
from sentry_backend.db.models.analytics_flow import FLOW_GRID
from sentry_backend.db.models.analytics_footfall import GRID_SIZE
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.feedback import Feedback
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import (
    get_current_organization_id,
    get_current_organization_id_admin,
)
from sentry_backend.repository import analytics_repo, camera_repo, store_repo
from sentry_backend.schemas.analytics import (
    DemographicSlice,
    DemographicsSummary,
    FlowEdge,
    FlowSummary,
    FootfallGrid,
    PathsSummary,
    PeakCell,
    PeakMatrix,
    RiskCell,
    RiskEpisodeRow,
    RiskPoint,
    RiskSummary,
    TrafficPoint,
    TrafficSummary,
    WalkedPath,
    ZoneActivity,
    ZoneBreakdown,
    ZoneFlowEdge,
    ZoneFlowNode,
    ZoneFlowSummary,
)
from sentry_backend.schemas.floor_plan import DEFAULT_PLAN_SIZE, FloorPlan
from sentry_backend.schemas.store import StoreCreate, StorePublic, StoreUpdate
from sentry_backend.services import live_provision
from sentry_backend.services.footfall_aggregator import point_in_polygon

# Cap the heatmap window at 90 days so a hostile `hours` can't scan the whole
# table; default 24h (rolling "today"-ish view).
_MAX_HOURS = 24 * 90

router = APIRouter(prefix="/api/v1/stores", tags=["stores"])


@router.get("", response_model=list[StorePublic])
async def list_stores(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> list[StorePublic]:
    stores = await store_repo.list_stores_for_org(db, org_id)
    return [StorePublic.model_validate(s) for s in stores]


@router.post("", response_model=StorePublic, status_code=status.HTTP_201_CREATED)
async def create_store(
    body: StoreCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> StorePublic:
    store = await store_repo.create_store(
        db,
        organization_id=org_id,
        name=body.name,
        address=body.address,
        timezone=body.timezone,
        telegram_chat_id=body.telegram_chat_id,
        staff_badge_color=body.staff_badge_color,
    )
    return StorePublic.model_validate(store)


@router.get("/{store_id}", response_model=StorePublic)
async def get_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> StorePublic:
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    return StorePublic.model_validate(store)


@router.patch("/{store_id}", response_model=StorePublic)
async def update_store(
    store_id: UUID,
    body: StoreUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> StorePublic:
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    prev_badge = store.staff_badge_color
    store = await store_repo.update_store(
        db,
        store,
        name=body.name,
        address=body.address,
        timezone=body.timezone,
        telegram_chat_id=body.telegram_chat_id,
        staff_badge_color=body.staff_badge_color,
    )
    # A badge-color change must reach the running workers now, not on the next
    # per-camera edit — re-provision the store's cloud cameras with the new color.
    if store.staff_badge_color != prev_badge:
        await _reprovision_store_cameras(db, store_id, org_id, store.staff_badge_color)
    return StorePublic.model_validate(store)


async def _reprovision_store_cameras(
    db: AsyncSession, store_id: UUID, org_id: UUID, staff_badge_color: str | None
) -> None:
    """Restart each enabled cloud camera's live worker so a store-level change
    (the staff badge color) applies immediately. Best-effort per camera; the
    node treats an identical spec as a no-op, so only the color change restarts."""
    cams = await camera_repo.list_cameras_for_org(db, org_id, store_id=store_id)
    for cam in cams:
        if cam.topology_mode != "cloud" or not cam.mediamtx_path or not cam.enabled:
            continue
        rtsp = await camera_repo.decrypt_rtsp_url(cam)
        if not rtsp:
            continue
        await live_provision.provision(
            cam.mediamtx_path,
            rtsp,
            enabled=cam.enabled,
            store_id=str(cam.store_id),
            risk_threshold=cam.risk_threshold,
            zones=cam.zones,
            staff_badge_color=staff_badge_color,
        )


@router.delete("/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> None:
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    await store_repo.delete_store(db, store)


# ── Floor plan (docs/30) — read-only for the frontend /insights viewport ────
# The plan is authored on agent-pc (agents.py PATCH /agent/floor-plan). This
# endpoint exposes it to the org's own dashboard so the analytics viewport can
# render walls, fixtures and camera icons over a top-down map. Empty plan =
# nothing drawn yet (frontend shows an empty state).
@router.get("/{store_id}/floor-plan", response_model=FloorPlan)
async def get_store_floor_plan(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> FloorPlan:
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    if store.floor_plan is None:
        return FloorPlan()
    return FloorPlan.model_validate(store.floor_plan)


# ── Footfall heatmap (docs/30 F2) — aggregated dwell grid for /insights ──────
@router.get("/{store_id}/analytics/footfall", response_model=FootfallGrid)
async def get_store_footfall(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24,
) -> FootfallGrid:
    """Dwell heatmap: person-frame samples per plan grid-cell over the last
    `hours` (default 24). Sparse — only non-zero cells are returned."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    cells = await analytics_repo.grid_for_store(db, store_id=store_id, start=start, end=end)

    size: tuple[float, float] = DEFAULT_PLAN_SIZE
    if store.floor_plan and isinstance(store.floor_plan.get("size"), (list, tuple)):
        raw = store.floor_plan["size"]
        with contextlib.suppress(TypeError, ValueError, IndexError):
            size = (float(raw[0]), float(raw[1]))

    max_samples = max((n for _, _, n in cells), default=0)
    total_samples = sum(n for _, _, n in cells)
    return FootfallGrid(
        grid_size=GRID_SIZE,
        size=size,
        window_from=start,
        window_to=end,
        max_samples=max_samples,
        total_samples=total_samples,
        cells=cells,
    )


# ── Visitor traffic (docs/30 F3) — hourly entries + peak for /insights KPIs ──
@router.get("/{store_id}/analytics/traffic", response_model=TrafficSummary)
async def get_store_traffic(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24,
) -> TrafficSummary:
    """Visitor count per hour over the last `hours` (default 24) — the headline
    "зочид" KPI + peak hour + an hourly series for the traffic chart."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    series = await analytics_repo.visits_hourly_for_store(
        db, store_id=store_id, start=start, end=end
    )
    total = sum(n for _, n in series)
    peak = max(series, key=lambda p: p[1], default=None)
    avg_dwell = await analytics_repo.avg_dwell_seconds_for_store(
        db, store_id=store_id, start=start, end=end
    )
    # Preceding window of equal length — powers the KPI trend badges.
    prev_start = start - timedelta(hours=hours)
    prev_series = await analytics_repo.visits_hourly_for_store(
        db, store_id=store_id, start=prev_start, end=start
    )
    prev_avg_dwell = await analytics_repo.avg_dwell_seconds_for_store(
        db, store_id=store_id, start=prev_start, end=start
    )
    return TrafficSummary(
        window_from=start,
        window_to=end,
        total=total,
        peak_hour=peak[0] if peak else None,
        peak_entries=peak[1] if peak else 0,
        avg_dwell_seconds=avg_dwell,
        prev_total=sum(n for _, n in prev_series),
        prev_avg_dwell_seconds=prev_avg_dwell,
        series=[TrafficPoint(hour=h, entries=n) for h, n in series],
    )


# ── Zone breakdown (docs/30 F4) — footfall attributed to each plan fixture ───
@router.get("/{store_id}/analytics/zones", response_model=ZoneBreakdown)
async def get_store_zones(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24,
) -> ZoneBreakdown:
    """Attribute the dwell heatmap to each drawn zone: for every plan fixture,
    sum the footfall samples whose grid-cell centre falls inside it. Reuses the
    F2 grid + the plan polygons — no extra aggregation path."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    cells = await analytics_repo.grid_for_store(db, store_id=store_id, start=start, end=end)

    plan = store.floor_plan or {}
    size = plan.get("size") or list(DEFAULT_PLAN_SIZE)
    try:
        sx, sy = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        sx, sy = DEFAULT_PLAN_SIZE
    fixtures = plan.get("fixtures") or []

    # Precompute each cell's centre in plan coords once.
    cell_centres = [
        ((gx + 0.5) / GRID_SIZE * sx, (gy + 0.5) / GRID_SIZE * sy, n) for gx, gy, n in cells
    ]

    zone_samples: list[tuple[str, str, str | None, int]] = []
    for idx, f in enumerate(fixtures):
        pts = f.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        total = sum(n for cx, cy, n in cell_centres if point_in_polygon(cx, cy, pts))
        if total > 0:
            raw_label = f.get("label")
            label = str(raw_label) if raw_label else None
            zone_samples.append(
                (str(f.get("id") or f"zone{idx}"), str(f.get("type") or "zone"), label, total)
            )

    zone_samples.sort(key=lambda z: z[3], reverse=True)
    grand = sum(n for _, _, _, n in zone_samples)
    return ZoneBreakdown(
        window_from=start,
        window_to=end,
        total_samples=grand,
        zones=[
            ZoneActivity(
                fixture_id=fid,
                type=ftype,
                label=label,
                samples=n,
                share=(n / grand) if grand else 0.0,
            )
            for fid, ftype, label, n in zone_samples
        ],
    )


# ── Movement flow (docs/30 F4 flow) — traffic lines between areas ────────────
@router.get("/{store_id}/analytics/flow", response_model=FlowSummary)
async def get_store_flow(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24,
) -> FlowSummary:
    """Movement graph: the busiest transitions between coarse plan cells over the
    last `hours`. Edge endpoints are cell centres normalized to [0,1] so the
    frontend draws lines directly over the plan."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    raw = await analytics_repo.flow_edges_for_store(db, store_id=store_id, start=start, end=end)

    def centre(idx: int) -> tuple[float, float]:
        fx, fy = idx % FLOW_GRID, idx // FLOW_GRID
        return (fx + 0.5) / FLOW_GRID, (fy + 0.5) / FLOW_GRID

    max_count = max((n for _, _, n in raw), default=0)
    edges = []
    for fc, tc, n in raw:
        x1, y1 = centre(fc)
        x2, y2 = centre(tc)
        edges.append(FlowEdge(x1=x1, y1=y1, x2=x2, y2=y2, count=n))
    return FlowSummary(
        grid_size=FLOW_GRID,
        window_from=start,
        window_to=end,
        max_count=max_count,
        edges=edges,
    )


_ZONE_TYPE_LABEL = {
    "shelf": "Тавиур",
    "fridge": "Хөргүүр",
    "mannequin": "Маникен",
    "checkout": "Касс",
    "exit": "Орц/Гарц",
    "entrance": "Орц",
}
_ZONE_FLOW_TOP_EDGES = 10


# ── Zone flow (docs/30 F4, zone level) — «Орц → Тавиур → Касс» graph ─────────
@router.get("/{store_id}/analytics/zone-flow", response_model=ZoneFlowSummary)
async def get_store_zone_flow(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24,
) -> ZoneFlowSummary:
    """The movement graph collapsed onto the operator-drawn plan fixtures.

    The raw FLOW_GRID lattice reads as grid geometry, not shopper behaviour.
    Here every cell is assigned to its containing fixture — or, for walkway
    cells, the nearest fixture centroid (a Voronoi partition) — and the
    cell-to-cell transition counts are summed per zone pair. Opposite
    directions cancel into net flow, so the frontend draws a handful of named
    arrows («Орц → Тавиур А») instead of a mesh."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    empty = ZoneFlowSummary(window_from=start, window_to=end, max_count=0)

    plan = store.floor_plan or {}
    size = plan.get("size") or DEFAULT_PLAN_SIZE
    try:
        pw, ph = float(size[0]) or 1.0, float(size[1]) or 1.0
    except (TypeError, ValueError, IndexError):
        pw, ph = DEFAULT_PLAN_SIZE

    nodes: list[ZoneFlowNode] = []
    polys: list[list[list[float]]] = []
    for i, f in enumerate(plan.get("fixtures") or []):
        ftype = str(f.get("type") or "")
        pts = f.get("points") or []
        if ftype == "furniture" or len(pts) < 3:
            continue  # scenery never carries flow semantics
        norm = [[float(x) / pw, float(y) / ph] for x, y in pts]
        cx = sum(p[0] for p in norm) / len(norm)
        cy = sum(p[1] for p in norm) / len(norm)
        label = str(f.get("label") or "") or f"{_ZONE_TYPE_LABEL.get(ftype, ftype)} {i + 1}"
        nodes.append(
            ZoneFlowNode(id=str(f.get("id") or f"z{i}"), label=label, type=ftype, x=cx, y=cy)
        )
        polys.append(norm)
    if len(nodes) < 2:
        return empty  # nothing to flow between — the frontend explains why

    def zone_of(cx: float, cy: float) -> int:
        for i, poly in enumerate(polys):
            if point_in_polygon(cx, cy, poly):
                return i
        best, best_d = 0, float("inf")
        for i, n in enumerate(nodes):
            d = (n.x - cx) ** 2 + (n.y - cy) ** 2
            if d < best_d:
                best, best_d = i, d
        return best

    cell_zone = [
        zone_of((c % FLOW_GRID + 0.5) / FLOW_GRID, (c // FLOW_GRID + 0.5) / FLOW_GRID)
        for c in range(FLOW_GRID * FLOW_GRID)
    ]

    raw = await analytics_repo.flow_edges_for_store(db, store_id=store_id, start=start, end=end)
    gross: dict[tuple[int, int], int] = {}
    for fc, tc, n in raw:
        za, zb = cell_zone[fc], cell_zone[tc]
        if za == zb:
            continue
        gross[(za, zb)] = gross.get((za, zb), 0) + n

    net: dict[tuple[int, int], int] = {}
    for (za, zb), n in gross.items():
        if (zb, za) in net:
            continue
        diff = n - gross.get((zb, za), 0)
        if diff > 0:
            net[(za, zb)] = diff
        elif diff < 0:
            net[(zb, za)] = -diff

    top = sorted(net.items(), key=lambda kv: kv[1], reverse=True)[:_ZONE_FLOW_TOP_EDGES]
    used = {i for (a, b), _ in top for i in (a, b)}
    # Gross in/out per zone across ALL edges (not just the top) — the summary
    # chips must reflect the whole window, not the visible subset.
    for (za, zb), n in gross.items():
        nodes[za].out_total += n
        nodes[zb].in_total += n
    return ZoneFlowSummary(
        window_from=start,
        window_to=end,
        max_count=top[0][1] if top else 0,
        nodes=[n for i, n in enumerate(nodes) if i in used],
        edges=[
            ZoneFlowEdge(
                from_id=nodes[a].id,
                to_id=nodes[b].id,
                count=n,
                back_count=gross.get((b, a), 0),
            )
            for (a, b), n in top
        ],
    )


# ── Walked paths (docs/30 F4 paths) — the spaghetti layer ────────────────────
@router.get("/{store_id}/analytics/paths", response_model=PathsSummary)
async def get_store_paths(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24,
    limit: Annotated[int, Query(ge=10, le=1000)] = 400,
) -> PathsSummary:
    """The most recent anonymous visitor paths over the window — thin dotted
    traces that show the store's REAL walking corridors, one line per visit."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    rows = await analytics_repo.paths_for_store(
        db, store_id=store_id, start=start, end=end, limit=limit
    )
    return PathsSummary(
        window_from=start,
        window_to=end,
        paths=[
            WalkedPath(started_at=ts, duration_sec=d, points=pts, gender=g, age_band=a)
            for ts, d, pts, g, a in rows
        ],
    )


# ── Risk analytics — where/when suspicious episodes cluster ─────────────────
@router.get("/{store_id}/analytics/risk", response_model=RiskSummary)
async def get_store_risk(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 168,
    min_pct: Annotated[float, Query(ge=0, le=100)] = 25.0,
) -> RiskSummary:
    """Risk-episode analytics over the window (default 7 days): plan heat points
    (episodes whose peak location projected onto the plan), weekday×hour timing
    cells, top firing behaviors/cameras, and the latest episodes. Sourced from
    the risk_episode activity-log rows the threshold handler writes.

    Aggregated in SQL — a busy store logs thousands of episodes per day, so
    fetching rows would silently truncate long windows. `min_pct` (default 25 =
    the HIGH band) filters out yellow-band noise; pass 0 for everything."""
    from zoneinfo import ZoneInfo

    from sentry_backend.db.models.event_log import EventLog, EventType

    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    prev_start = start - timedelta(hours=hours)
    tz_name = store.timezone or "UTC"
    try:
        ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — a bad stored tz must not 500 analytics
        tz_name = "UTC"

    from sqlalchemy import Float, cast, func, select, true

    pct_expr = cast(EventLog.detail["peak_risk_pct"].astext, Float)
    base = (
        (EventLog.store_id == store_id)
        & (EventLog.event_type == EventType.risk_episode)
        & (func.coalesce(pct_expr, 0.0) >= min_pct)
    )
    in_window = base & (EventLog.created_at >= start)

    total = (
        await db.execute(select(func.count()).select_from(EventLog).where(in_window))
    ).scalar() or 0
    alerted_n = (
        await db.execute(
            select(func.count())
            .select_from(EventLog)
            .where(in_window, EventLog.detail["alerted"].astext == "true")
        )
    ).scalar() or 0
    prev_total = (
        await db.execute(
            select(func.count())
            .select_from(EventLog)
            .where(base, EventLog.created_at >= prev_start, EventLog.created_at < start)
        )
    ).scalar() or 0

    # Weekday × hour cells in STORE-local time, aggregated by Postgres.
    local_ts = func.timezone(tz_name, EventLog.created_at)
    dow_expr = cast(func.extract("isodow", local_ts), Float)
    hour_expr = cast(func.extract("hour", local_ts), Float)
    cell_rows = (
        await db.execute(
            select(dow_expr, hour_expr, func.count()).where(in_window).group_by(dow_expr, hour_expr)
        )
    ).all()
    cell_counts = {(int(d_), int(h)): int(n) for d_, h, n in cell_rows}

    # Top behaviors: unnest the JSONB behaviors array server-side.
    beh = func.jsonb_array_elements_text(EventLog.detail["behaviors"]).column_valued("beh")
    beh_rows = (
        await db.execute(
            select(beh, func.count())
            .select_from(EventLog)
            .where(in_window, func.jsonb_typeof(EventLog.detail["behaviors"]) == "array")
            .group_by(beh)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    from sentry_backend.db.models.camera import Camera

    cam_names = {
        c.id: c.name
        for c in (await db.execute(select(Camera).where(Camera.store_id == store_id)))
        .scalars()
        .all()
    }
    cam_rows2 = (
        await db.execute(
            select(EventLog.camera_id, func.count())
            .where(in_window)
            .group_by(EventLog.camera_id)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    # Plan heat points: newest located episodes only (bounded — the layer draws
    # dots, a few hundred is already a dense picture).
    point_rows = (
        (
            await db.execute(
                select(EventLog)
                .where(in_window, EventLog.detail["plan_pos"].astext.isnot(None), true())
                .order_by(EventLog.created_at.desc())
                .limit(1500)
            )
        )
        .scalars()
        .all()
    )
    points: list[RiskPoint] = []
    for r in point_rows:
        d = r.detail or {}
        pos = d.get("plan_pos")
        if isinstance(pos, list) and len(pos) == 2:
            with contextlib.suppress(TypeError, ValueError):
                points.append(
                    RiskPoint(
                        x=float(pos[0]), y=float(pos[1]), pct=float(d.get("peak_risk_pct") or 0.0)
                    )
                )

    recent_rows = (
        (
            await db.execute(
                select(EventLog).where(in_window).order_by(EventLog.created_at.desc()).limit(20)
            )
        )
        .scalars()
        .all()
    )
    recent = [
        RiskEpisodeRow(
            ts=r.created_at,
            camera_name=cam_names.get(r.camera_id, "?") if r.camera_id else "?",
            peak_risk_pct=float((r.detail or {}).get("peak_risk_pct") or 0.0),
            level=str((r.detail or {}).get("level") or "MEDIUM"),
            behaviors=[str(b) for b in ((r.detail or {}).get("behaviors") or [])],
            alerted=bool((r.detail or {}).get("alerted")),
            duration_sec=float((r.detail or {}).get("duration_sec") or 0.0),
        )
        for r in recent_rows
    ]

    def top(counts: list[tuple[Any, int]], name: bool = False) -> list[DemographicSlice]:
        out = []
        for k, n in counts:
            label = (cam_names.get(k, "?") if name else str(k)) if k is not None else "?"
            out.append(DemographicSlice(key=label, count=int(n), share=(n / total) if total else 0))
        return out

    return RiskSummary(
        window_from=start,
        window_to=end,
        timezone=tz_name,
        total=int(total),
        alerted=int(alerted_n),
        prev_total=int(prev_total),
        max_cell=max(cell_counts.values(), default=0),
        cells=[RiskCell(dow=d_, hour=h, count=n) for (d_, h), n in sorted(cell_counts.items())],
        points=points,
        top_behaviors=top([(k, n) for k, n in beh_rows]),
        top_cameras=top([(k, n) for k, n in cam_rows2], name=True),
        recent=recent,
    )


# ── Demographics (docs/30 F5) — gender/age structure of classified visitors ──
@router.get("/{store_id}/analytics/demographics", response_model=DemographicsSummary)
async def get_store_demographics(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24,
) -> DemographicsSummary:
    """Gender + age-band split of classified visitors over the last `hours`.
    Counts come from optional per-track classifier attributes on the live
    stream (LiveTrack.gender/age_band); a store whose node runs no
    demographics model returns total=0."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    rows = await analytics_repo.demographics_for_store(db, store_id=store_id, start=start, end=end)
    total = sum(n for _, _, n in rows)

    def slices(idx: int) -> list[DemographicSlice]:
        agg: dict[str, int] = {}
        for row in rows:
            key = str(row[idx])
            agg[key] = agg.get(key, 0) + row[2]
        return [
            DemographicSlice(key=k, count=n, share=(n / total) if total else 0.0)
            for k, n in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
            if n > 0
        ]

    return DemographicsSummary(
        window_from=start,
        window_to=end,
        total=total,
        gender=slices(0),
        age=slices(1),
    )


# ── Peak-hour matrix (docs/30) — weekday × hour visitor pattern ──────────────
@router.get("/{store_id}/analytics/peak", response_model=PeakMatrix)
async def get_store_peak(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    days: Annotated[int, Query(ge=1, le=90)] = 28,
) -> PeakMatrix:
    """When is the store busy? Visitors bucketed by (weekday, hour) in the
    store's local timezone over the last `days` (default 28 = 4 weeks)."""
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")

    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    tz = store.timezone or "UTC"
    cells = await analytics_repo.visits_by_dow_hour(
        db, store_id=store_id, start=start, end=end, tz=tz
    )
    max_entries = max((n for _, _, n in cells), default=0)
    return PeakMatrix(
        window_from=start,
        window_to=end,
        timezone=tz,
        max_entries=max_entries,
        cells=[PeakCell(dow=d, hour=h, entries=n) for d, h, n in cells],
    )


# Store agents seen within this window are "reporting"; a stale agent means its
# per-camera push flags can't be trusted → those cameras are "unknown".
_AGENT_ONLINE_WINDOW = timedelta(minutes=3)


@router.get("/{store_id}/analytics/system-health")
async def get_store_system_health(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    hours: Annotated[int, Query(ge=1, le=_MAX_HOURS)] = 24 * 30,
) -> dict[str, object]:
    """Per-store system-quality snapshot for the analytics page.

    - cameras: live availability (streaming now / enabled) + the offline list
      with the real reason. No heartbeat time-series is stored, so this is a
      CURRENT-state number, not a historical uptime %.
    - precision / false-alarm rate from staff feedback on THIS store's alerts.
    - response_time: alert → first staff feedback (no separate acknowledge
      action exists), median + mean minutes.
    Recall is intentionally absent — a missed theft leaves nothing to count.
    """
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    now = datetime.now(UTC)
    frm = now - timedelta(hours=hours)

    # ── Cameras: match this store's enabled cameras to its agents' push state ──
    cam_rows = (
        await db.execute(
            select(Camera.mediamtx_path, Camera.name).where(
                Camera.store_id == store_id, Camera.enabled.is_(True)
            )
        )
    ).all()
    agents = (
        await db.execute(
            select(Agent).where(Agent.store_id == store_id, Agent.is_active.is_(True))
        )
    ).scalars().all()
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
    for path, cam_name in cam_rows:
        st = push_by_path.get(path) if path else None
        if st is None:
            unknown += 1
        elif st["fresh"] and st["running"]:
            online += 1
        else:
            offline += 1
            offline_list.append(
                {
                    "camera": cam_name,
                    "reason": "Агент офлайн"
                    if not st["fresh"]
                    else (st["last_error"] or "Дамжуулал зогссон"),
                }
            )
    measurable = online + offline
    cameras = {
        "total_enabled": len(cam_rows),
        "online": online,
        "offline": offline,
        "unknown": unknown,
        "availability_pct": round(100.0 * online / measurable, 1) if measurable else None,
        "offline_list": offline_list[:50],
    }

    # ── Precision / false-alarm rate from this store's alert feedback ──
    fb_rows = (
        await db.execute(
            select(Alert.id, Feedback.verdict, Feedback.created_at)
            .join(Feedback, Feedback.alert_id == Alert.id)
            .where(Alert.store_id == store_id, Alert.created_at >= frm)
        )
    ).all()
    latest: dict[Any, str] = {}
    latest_ts: dict[Any, datetime] = {}
    for aid, verdict, fts in fb_rows:
        if aid not in latest_ts or fts > latest_ts[aid]:
            latest_ts[aid] = fts
            latest[aid] = str(verdict)
    tp = sum(1 for v in latest.values() if v == "true_positive")
    fp = sum(1 for v in latest.values() if v == "false_positive")
    unclear = sum(1 for v in latest.values() if v not in ("true_positive", "false_positive"))
    total_alerts = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.store_id == store_id, Alert.created_at >= frm)
            )
        ).scalar()
        or 0
    )
    labeled = len(latest)
    quality = {
        "total_alerts": total_alerts,
        "labeled": labeled,
        "tp": tp,
        "fp": fp,
        "unclear": unclear,
        "precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
        "fp_rate": round(fp / labeled, 3) if labeled else None,
    }

    # ── Alert → response time (first feedback proxy) ──
    delta_rows = (
        await db.execute(
            select(Alert.created_at, func.min(Feedback.created_at))
            .join(Feedback, Feedback.alert_id == Alert.id)
            .where(Alert.store_id == store_id, Alert.created_at >= frm)
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

    return {"cameras": cameras, "quality": quality, "response_time": response_time}
