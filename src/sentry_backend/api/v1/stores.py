"""Stores router — org-scoped CRUD."""

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.analytics_flow import FLOW_GRID
from sentry_backend.db.models.analytics_footfall import GRID_SIZE
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import (
    get_current_organization_id,
    get_current_organization_id_admin,
)
from sentry_backend.repository import analytics_repo, store_repo
from sentry_backend.schemas.analytics import (
    FlowEdge,
    FlowSummary,
    FootfallGrid,
    PeakCell,
    PeakMatrix,
    TrafficPoint,
    TrafficSummary,
    ZoneActivity,
    ZoneBreakdown,
)
from sentry_backend.schemas.floor_plan import FloorPlan
from sentry_backend.schemas.store import StoreCreate, StorePublic, StoreUpdate
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
    store = await store_repo.update_store(
        db,
        store,
        name=body.name,
        address=body.address,
        timezone=body.timezone,
        telegram_chat_id=body.telegram_chat_id,
    )
    return StorePublic.model_validate(store)


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

    size: tuple[float, float] = (1000.0, 800.0)
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
    return TrafficSummary(
        window_from=start,
        window_to=end,
        total=total,
        peak_hour=peak[0] if peak else None,
        peak_entries=peak[1] if peak else 0,
        avg_dwell_seconds=avg_dwell,
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
    size = plan.get("size") or [1000.0, 800.0]
    try:
        sx, sy = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        sx, sy = 1000.0, 800.0
    fixtures = plan.get("fixtures") or []

    # Precompute each cell's centre in plan coords once.
    cell_centres = [
        ((gx + 0.5) / GRID_SIZE * sx, (gy + 0.5) / GRID_SIZE * sy, n) for gx, gy, n in cells
    ]

    zone_samples: list[tuple[str, str, int]] = []
    for idx, f in enumerate(fixtures):
        pts = f.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        total = sum(n for cx, cy, n in cell_centres if point_in_polygon(cx, cy, pts))
        if total > 0:
            zone_samples.append(
                (str(f.get("id") or f"zone{idx}"), str(f.get("type") or "zone"), total)
            )

    zone_samples.sort(key=lambda z: z[2], reverse=True)
    grand = sum(n for _, _, n in zone_samples)
    return ZoneBreakdown(
        window_from=start,
        window_to=end,
        total_samples=grand,
        zones=[
            ZoneActivity(fixture_id=fid, type=ftype, samples=n, share=(n / grand) if grand else 0.0)
            for fid, ftype, n in zone_samples
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
