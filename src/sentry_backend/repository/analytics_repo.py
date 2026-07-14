"""Footfall analytics persistence (docs/30 F2).

`bump_buckets` upserts a batch of per-cell increments (called by the in-process
aggregator and by any future edge push). `grid_for_store` sums a time window
back into a single GRID×GRID heatmap for the /insights viewport.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.analytics_demographics import AnalyticsDemographics
from sentry_backend.db.models.analytics_dwell import AnalyticsDwell
from sentry_backend.db.models.analytics_flow import AnalyticsFlow
from sentry_backend.db.models.analytics_path import AnalyticsPath
from sentry_backend.db.models.analytics_footfall import AnalyticsFootfall
from sentry_backend.db.models.analytics_visit import AnalyticsVisit


class BucketDelta:
    """One (camera, hour, cell) increment. Plain object (not pydantic) — this is
    a hot in-process path, no validation needed."""

    __slots__ = ("camera_id", "hour_ts", "grid_x", "grid_y", "samples", "gender_m", "gender_f")

    def __init__(
        self,
        camera_id: str,
        hour_ts: datetime,
        grid_x: int,
        grid_y: int,
        samples: int,
        gender_m: int = 0,
        gender_f: int = 0,
    ) -> None:
        self.camera_id = camera_id
        self.hour_ts = hour_ts
        self.grid_x = grid_x
        self.grid_y = grid_y
        self.samples = samples
        self.gender_m = gender_m
        self.gender_f = gender_f


async def bump_buckets(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    deltas: list[BucketDelta],
) -> None:
    """Upsert-increment a batch of footfall buckets. Idempotent per row via the
    (store, camera, hour, cell) unique constraint — concurrent flushes ADD."""
    if not deltas:
        return
    rows = [
        {
            "organization_id": organization_id,
            "store_id": store_id,
            "camera_id": d.camera_id,
            "hour_ts": d.hour_ts,
            "grid_x": d.grid_x,
            "grid_y": d.grid_y,
            "samples": d.samples,
            "gender_m": d.gender_m,
            "gender_f": d.gender_f,
        }
        for d in deltas
    ]
    stmt = pg_insert(AnalyticsFootfall).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_footfall_bucket",
        set_={
            "samples": AnalyticsFootfall.samples + stmt.excluded.samples,
            "gender_m": AnalyticsFootfall.gender_m + stmt.excluded.gender_m,
            "gender_f": AnalyticsFootfall.gender_f + stmt.excluded.gender_f,
        },
    )
    await db.execute(stmt)


async def grid_for_store(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
) -> list[tuple[int, int, int]]:
    """Sum every camera+hour bucket in [start, end) into (grid_x, grid_y, samples)
    cells for the whole store. Cells with zero samples are simply absent."""
    result = await db.execute(
        select(
            AnalyticsFootfall.grid_x,
            AnalyticsFootfall.grid_y,
            func.sum(AnalyticsFootfall.samples).label("n"),
        )
        .where(
            AnalyticsFootfall.store_id == store_id,
            AnalyticsFootfall.hour_ts >= start,
            AnalyticsFootfall.hour_ts < end,
        )
        .group_by(AnalyticsFootfall.grid_x, AnalyticsFootfall.grid_y)
    )
    return [(gx, gy, int(n)) for gx, gy, n in result.all()]


async def bump_visits(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    counts: list[tuple[str, datetime, int]],
) -> None:
    """Upsert-increment hourly visitor counts. Each tuple is (camera_id, hour, n)."""
    if not counts:
        return
    rows = [
        {
            "organization_id": organization_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "hour_ts": hour_ts,
            "entries": n,
        }
        for camera_id, hour_ts, n in counts
    ]
    stmt = pg_insert(AnalyticsVisit).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_visit_bucket",
        set_={"entries": AnalyticsVisit.entries + stmt.excluded.entries},
    )
    await db.execute(stmt)


async def bump_demographics(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    counts: list[tuple[str, datetime, str, str, int]],
) -> None:
    """Upsert-increment hourly demographics counts (docs/30 F5). Each tuple is
    (camera_id, hour, gender, age_band, n)."""
    if not counts:
        return
    rows = [
        {
            "organization_id": organization_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "hour_ts": hour_ts,
            "gender": gender,
            "age_band": age_band,
            "count": n,
        }
        for camera_id, hour_ts, gender, age_band, n in counts
    ]
    stmt = pg_insert(AnalyticsDemographics).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_demographics_bucket",
        set_={"count": AnalyticsDemographics.count + stmt.excluded.count},
    )
    await db.execute(stmt)


async def demographics_for_store(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
) -> list[tuple[str, str, int]]:
    """Sum the window into (gender, age_band, count) slices for one store."""
    result = await db.execute(
        select(
            AnalyticsDemographics.gender,
            AnalyticsDemographics.age_band,
            func.sum(AnalyticsDemographics.count).label("n"),
        )
        .where(
            AnalyticsDemographics.store_id == store_id,
            AnalyticsDemographics.hour_ts >= start,
            AnalyticsDemographics.hour_ts < end,
        )
        .group_by(AnalyticsDemographics.gender, AnalyticsDemographics.age_band)
    )
    return [(g, a, int(n)) for g, a, n in result.all()]


async def visits_hourly_for_store(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, int]]:
    """Entries per hour across all cameras in [start, end), oldest first."""
    result = await db.execute(
        select(
            AnalyticsVisit.hour_ts,
            func.sum(AnalyticsVisit.entries).label("n"),
        )
        .where(
            AnalyticsVisit.store_id == store_id,
            AnalyticsVisit.hour_ts >= start,
            AnalyticsVisit.hour_ts < end,
        )
        .group_by(AnalyticsVisit.hour_ts)
        .order_by(AnalyticsVisit.hour_ts)
    )
    return [(hour, int(n)) for hour, n in result.all()]


async def bump_dwell(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    counts: list[tuple[str, datetime, int, int]],
) -> None:
    """Upsert-add completed-track dwell. Each tuple is
    (camera_id, hour, track_count, dwell_ms_sum)."""
    if not counts:
        return
    rows = [
        {
            "organization_id": organization_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "hour_ts": hour_ts,
            "track_count": tc,
            "dwell_ms_sum": dms,
        }
        for camera_id, hour_ts, tc, dms in counts
    ]
    stmt = pg_insert(AnalyticsDwell).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_dwell_bucket",
        set_={
            "track_count": AnalyticsDwell.track_count + stmt.excluded.track_count,
            "dwell_ms_sum": AnalyticsDwell.dwell_ms_sum + stmt.excluded.dwell_ms_sum,
        },
    )
    await db.execute(stmt)


async def avg_dwell_seconds_for_store(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
) -> float | None:
    """Mean dwell (seconds) across all completed tracks in [start, end), or None
    if no tracks completed in the window."""
    result = await db.execute(
        select(
            func.sum(AnalyticsDwell.dwell_ms_sum),
            func.sum(AnalyticsDwell.track_count),
        ).where(
            AnalyticsDwell.store_id == store_id,
            AnalyticsDwell.hour_ts >= start,
            AnalyticsDwell.hour_ts < end,
        )
    )
    ms_sum, tracks = result.one()
    if not tracks:
        return None
    return (int(ms_sum) / int(tracks)) / 1000.0


async def bump_flow(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    edges: list[tuple[str, datetime, int, int, int]],
) -> None:
    """Upsert-increment movement edges. Each tuple is
    (camera_id, hour, from_cell, to_cell, count)."""
    if not edges:
        return
    rows = [
        {
            "organization_id": organization_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "hour_ts": hour_ts,
            "from_cell": fc,
            "to_cell": tc,
            "count": n,
        }
        for camera_id, hour_ts, fc, tc, n in edges
    ]
    stmt = pg_insert(AnalyticsFlow).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_flow_edge",
        set_={"count": AnalyticsFlow.count + stmt.excluded.count},
    )
    await db.execute(stmt)


async def flow_edges_for_store(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
    limit: int = 200,
) -> list[tuple[int, int, int]]:
    """Top movement edges (from_cell, to_cell, count) in [start, end), busiest
    first — summed across cameras + hours."""
    result = await db.execute(
        select(
            AnalyticsFlow.from_cell,
            AnalyticsFlow.to_cell,
            func.sum(AnalyticsFlow.count).label("n"),
        )
        .where(
            AnalyticsFlow.store_id == store_id,
            AnalyticsFlow.hour_ts >= start,
            AnalyticsFlow.hour_ts < end,
        )
        .group_by(AnalyticsFlow.from_cell, AnalyticsFlow.to_cell)
        .order_by(func.sum(AnalyticsFlow.count).desc())
        .limit(limit)
    )
    return [(fc, tc, int(n)) for fc, tc, n in result.all()]


async def insert_paths(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    rows: list[tuple[str, datetime, float, list[list[float]]]],
) -> None:
    """Insert finished walked paths. Each tuple is
    (camera_id, started_at, duration_sec, points)."""
    if not rows:
        return
    await db.execute(
        pg_insert(AnalyticsPath).values(
            [
                {
                    "organization_id": organization_id,
                    "store_id": store_id,
                    "camera_id": camera_id,
                    "started_at": started_at,
                    "duration_sec": round(dur, 1),
                    "points": pts,
                    "n_points": len(pts),
                }
                for camera_id, started_at, dur, pts in rows
            ]
        )
    )


async def paths_for_store(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
    limit: int = 400,
) -> list[tuple[datetime, float, list[list[float]]]]:
    """Most recent walked paths (started_at, duration_sec, points) in the
    window, newest first, row-capped so the spaghetti layer stays light."""
    result = await db.execute(
        select(AnalyticsPath.started_at, AnalyticsPath.duration_sec, AnalyticsPath.points)
        .where(
            AnalyticsPath.store_id == store_id,
            AnalyticsPath.started_at >= start,
            AnalyticsPath.started_at < end,
        )
        .order_by(AnalyticsPath.started_at.desc())
        .limit(limit)
    )
    return [(ts, float(d), pts) for ts, d, pts in result.all()]


async def visits_by_dow_hour(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
    tz: str,
) -> list[tuple[int, int, int]]:
    """Entries bucketed by (isodow 1-7 Mon-Sun, hour 0-23) in the store's local
    timezone over [start, end). One row per non-empty (dow, hour) cell."""
    local = func.timezone(tz, AnalyticsVisit.hour_ts)
    dow = func.extract("isodow", local)
    hr = func.extract("hour", local)
    result = await db.execute(
        select(dow, hr, func.sum(AnalyticsVisit.entries))
        .where(
            AnalyticsVisit.store_id == store_id,
            AnalyticsVisit.hour_ts >= start,
            AnalyticsVisit.hour_ts < end,
        )
        .group_by(dow, hr)
    )
    return [(int(d), int(h), int(n)) for d, h, n in result.all()]


async def total_samples_for_store(
    db: AsyncSession,
    *,
    store_id: UUID,
    start: datetime,
    end: datetime,
) -> int:
    """Sum of all samples in the window — a coarse activity total for KPI copy."""
    result = await db.execute(
        select(func.coalesce(func.sum(AnalyticsFootfall.samples), 0)).where(
            AnalyticsFootfall.store_id == store_id,
            AnalyticsFootfall.hour_ts >= start,
            AnalyticsFootfall.hour_ts < end,
        )
    )
    return int(result.scalar_one())
