"""Footfall aggregator (docs/30 F2).

Taps the existing live-metadata firehose (POST /internal/live-metadata), turns
each tracked person's foot point into a plan-normalized grid cell, and buffers
per-cell counts in memory. Buckets flush to `analytics_footfall` in batches so
the hot path never blocks on the DB. Privacy-safe: only per-cell counts persist.

Projection uses the camera's homography (from Store.floor_plan) when calibrated
(docs/30 Phase B); before calibration it falls back to a coarse blob anchored at
the camera's placed position so the heatmap is still useful day one.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from sentry_backend.db.models.analytics_flow import FLOW_GRID
from sentry_backend.db.models.analytics_footfall import GRID_SIZE
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.store import Store
from sentry_backend.db.session import session_scope
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import analytics_repo
from sentry_backend.repository.analytics_repo import BucketDelta

log = get_logger(__name__)

# Flush when the buffer holds this many distinct cells, or this many seconds
# since the last flush — whichever comes first. Keeps memory + write frequency
# bounded on a busy store without a background task.
_FLUSH_CELLS = 800
_FLUSH_SECONDS = 90.0
# Camera→tenancy/plan resolution cache TTL. The plan changes rarely; a short TTL
# keeps the hot path off the DB while still picking up a re-calibration.
_RESOLVE_TTL = 120.0
# Fallback (uncalibrated) blob radius as a fraction of the plan's shorter side.
_FALLBACK_RADIUS_FRAC = 0.28
# A track continuously inside a gate zone counts once; once it's been gone this
# long it's forgotten so a real re-entry recounts. Also the prune horizon.
_GATE_TTL = 45.0
# A track "completes" (its dwell is banked) once it's been absent this long.
_PRESENCE_TTL = 45.0


def _clamp_cell(v: float) -> int:
    return max(0, min(GRID_SIZE - 1, int(v * GRID_SIZE)))


def project_foot_to_cell(
    plan: dict[str, Any] | None,
    camera_id: str,
    foot_nx: float,
    foot_ny: float,
) -> tuple[int, int] | None:
    """Map a normalized (0-1) camera-image foot point to a plan grid cell.

    Returns None when the camera isn't placed on the plan (can't be located).
    """
    point = project_foot_to_plan(plan, camera_id, foot_nx, foot_ny)
    if point is None:
        return None
    (px, py), (sx, sy) = point
    return _clamp_cell(px / sx), _clamp_cell(py / sy)


def project_foot_to_plan(
    plan: dict[str, Any] | None,
    camera_id: str,
    foot_nx: float,
    foot_ny: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Map a normalized (0-1) camera-image foot point to ABSOLUTE plan coords.

    Returns ((plan_x, plan_y), (size_x, size_y)) clipped into the plan bounds, or
    None when the camera isn't placed on the plan. Absolute coords let callers do
    point-in-polygon against plan fixtures (F3 gate counting) as well as gridding.
    """
    if not plan:
        return None
    size = plan.get("size") or [1000.0, 800.0]
    try:
        sx, sy = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    if sx <= 0 or sy <= 0:
        return None

    cam = None
    for c in plan.get("cameras") or []:
        if c.get("camera_id") == camera_id:
            cam = c
            break
    if cam is None:
        return None

    homography = cam.get("homography")
    if homography:
        # H · [nx, ny, 1] → plan coords.
        try:
            (a, b, c0), (d, e, f), (g, h, i) = homography
            w = g * foot_nx + h * foot_ny + i
            if abs(w) < 1e-9:
                return None
            px = (a * foot_nx + b * foot_ny + c0) / w
            py = (d * foot_nx + e * foot_ny + f) / w
        except (TypeError, ValueError):
            return None
    else:
        # Pre-calibration fallback: spread footfall in a blob around the camera's
        # placed position. Rough, but shows "activity near this camera" day one.
        pos = cam.get("pos") or [sx / 2, sy / 2]
        try:
            cxp, cyp = float(pos[0]), float(pos[1])
        except (TypeError, ValueError, IndexError):
            return None
        radius = _FALLBACK_RADIUS_FRAC * min(sx, sy)
        px = cxp + (foot_nx - 0.5) * radius
        py = cyp + (foot_ny - 0.5) * radius

    # Clip into bounds rather than dropping — a slightly-off homography
    # shouldn't silently lose every edge point.
    px = min(sx - 1e-3, max(0.0, px))
    py = min(sy - 1e-3, max(0.0, py))
    return (px, py), (sx, sy)


def _flow_adjacent(a: int, b: int) -> bool:
    """True if two FLOW_GRID cell indices are within a 2-cell Chebyshev step —
    filters out tracking teleports / re-id jumps from the movement graph."""
    ax, ay = a % FLOW_GRID, a // FLOW_GRID
    bx, by = b % FLOW_GRID, b // FLOW_GRID
    return abs(ax - bx) <= 2 and abs(ay - by) <= 2


def point_in_polygon(px: float, py: float, points: list[Any]) -> bool:
    """Ray-casting point-in-polygon. `points` are [x, y] plan coords."""
    n = len(points)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for k in range(n):
        try:
            xk, yk = float(points[k][0]), float(points[k][1])
            xj, yj = float(points[j][0]), float(points[j][1])
        except (TypeError, ValueError, IndexError):
            return False
        if ((yk > py) != (yj > py)) and (px < (xj - xk) * (py - yk) / ((yj - yk) or 1e-9) + xk):
            inside = not inside
        j = k
    return inside


def _foot_norm(box: Any, width: int, height: int) -> tuple[float, float] | None:
    """Bottom-center of the bbox, normalized to [0,1]. Handles both pixel boxes
    (with frame width/height) and already-normalized boxes."""
    try:
        x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    except (TypeError, ValueError, IndexError):
        return None
    cx = (x1 + x2) / 2.0
    by = max(y1, y2)
    if width and height and max(x1, y1, x2, y2) > 1.5:
        return cx / width, by / height
    # Already normalized (0-1) — clamp defensively.
    return min(1.0, max(0.0, cx)), min(1.0, max(0.0, by))


class _Resolved:
    __slots__ = ("org_id", "store_id", "plan", "gates", "at")

    def __init__(
        self,
        org_id: UUID | None,
        store_id: UUID | None,
        plan: dict[str, Any] | None,
        at: float,
        gates: list[tuple[str, list[Any]]] | None = None,
    ) -> None:
        self.org_id = org_id
        self.store_id = store_id
        self.plan = plan
        # Precomputed gate fixtures (entrance/exit) as (fixture_id, points) — the
        # zones a track must enter to count as a visit (F3).
        self.gates = gates or []
        self.at = at


class _Presence:
    """A tracked person's active span, for dwell-time (F3+). Duration uses the
    monotonic clock (robust to missing/garbled frame timestamps); the hour bucket
    is stamped at first-seen from the frame wall time. `last_flow_cell` is the
    coarse FLOW_GRID cell last occupied, for movement-edge counting."""

    __slots__ = ("first_mono", "last_mono", "store_id", "hour", "last_flow_cell")

    def __init__(self, first_mono: float, store_id: UUID, hour: datetime) -> None:
        self.first_mono = first_mono
        self.last_mono = first_mono
        self.store_id = store_id
        self.hour = hour
        self.last_flow_cell: int | None = None


def _extract_gates(plan: dict[str, Any] | None) -> list[tuple[str, list[Any]]]:
    gates: list[tuple[str, list[Any]]] = []
    if not plan:
        return gates
    for idx, f in enumerate(plan.get("fixtures") or []):
        if f.get("type") in ("entrance", "exit"):
            pts = f.get("points")
            if isinstance(pts, list) and len(pts) >= 3:
                gates.append((str(f.get("id") or f"gate{idx}"), pts))
    return gates


class FootfallAggregator:
    def __init__(self) -> None:
        # (store_id, camera_id, hour_ts, gx, gy) → sample count
        self._buf: dict[tuple[UUID, str, datetime, int, int], int] = {}
        # (store_id, camera_id, hour_ts) → visitor count (F3 gate crossings)
        self._visits: dict[tuple[UUID, str, datetime], int] = {}
        # (camera_id, person_id) → last monotonic time seen inside a gate. A track
        # counts as ONE visit while continuously in the zone; once it leaves for
        # more than _GATE_TTL it's forgotten, so a genuine re-entry recounts.
        self._gate_seen: dict[tuple[str, int], float] = {}
        self._gate_last_prune = time.monotonic()
        # (camera_id, person_id) → active presence span (dwell-time, F3+).
        self._presence: dict[tuple[str, int], _Presence] = {}
        # (store_id, camera_id, hour) → [completed_track_count, dwell_ms_sum].
        self._dwell_buf: dict[tuple[UUID, str, datetime], list[int]] = {}
        # (store_id, camera_id, hour, from_cell, to_cell) → transition count.
        self._flow_buf: dict[tuple[UUID, str, datetime, int, int], int] = {}
        # store_id → org_id (for the flush write)
        self._store_org: dict[UUID, UUID] = {}
        self._resolve: dict[str, _Resolved] = {}
        self._lock = asyncio.Lock()
        self._last_flush = time.monotonic()

    async def on_frame(self, frame: dict[str, Any]) -> None:
        """Called from receive_live_metadata for each frame. Never raises."""
        try:
            await self._on_frame_inner(frame)
        except Exception:  # noqa: BLE001
            log.exception("footfall.on_frame_failed")

    async def _resolve_camera(self, camera_id: str) -> _Resolved | None:
        now = time.monotonic()
        cached = self._resolve.get(camera_id)
        if cached is not None and now - cached.at < _RESOLVE_TTL:
            return cached
        async with session_scope() as db:
            cam = (
                await db.execute(select(Camera).where(Camera.mediamtx_path == camera_id))
            ).scalar_one_or_none()
            if cam is None:
                resolved = _Resolved(None, None, None, now)
                self._resolve[camera_id] = resolved
                return resolved
            store = await db.get(Store, cam.store_id)
            org_id = store.organization_id if store else None
            plan = store.floor_plan if store else None
            resolved = _Resolved(org_id, cam.store_id, plan, now, _extract_gates(plan))
            self._resolve[camera_id] = resolved
            if org_id is not None:
                self._store_org[cam.store_id] = org_id
            return resolved

    async def _on_frame_inner(self, frame: dict[str, Any]) -> None:
        cam_path = frame.get("camera_id")
        if not isinstance(cam_path, str) or not cam_path:
            return
        tracks = frame.get("tracks") or []
        if not isinstance(tracks, list) or not tracks:
            return

        resolved = await self._resolve_camera(cam_path)
        if resolved is None or resolved.store_id is None or resolved.plan is None:
            return

        width = int(frame.get("width") or 0)
        height = int(frame.get("height") or 0)
        ts_ms = int(frame.get("ts_ms") or 0)
        hour = _hour_bucket(ts_ms)
        now = time.monotonic()

        pending: list[tuple[tuple[UUID, str, datetime, int, int], int]] = []
        # (camera, pid) that are inside a gate this frame, and whether it's a
        # fresh visit (first time we've seen this track in a gate).
        gate_hits: list[tuple[str, int, bool]] = []
        # person_ids seen this frame (for dwell presence tracking).
        present_pids: list[int] = []
        # (pid, coarse flow-cell index) for movement-edge counting.
        track_cells: list[tuple[int, int]] = []
        for t in tracks:
            pid = t.get("person_id")
            if isinstance(pid, int):
                present_pids.append(pid)
            foot = _foot_norm(t.get("box"), width, height)
            if foot is None:
                continue
            proj = project_foot_to_plan(resolved.plan, cam_path, foot[0], foot[1])
            if proj is None:
                continue
            (px, py), (sx, sy) = proj
            pending.append(
                (
                    (resolved.store_id, cam_path, hour, _clamp_cell(px / sx), _clamp_cell(py / sy)),
                    1,
                )
            )
            if isinstance(pid, int):
                fx = max(0, min(FLOW_GRID - 1, int(px / sx * FLOW_GRID)))
                fy = max(0, min(FLOW_GRID - 1, int(py / sy * FLOW_GRID)))
                track_cells.append((pid, fy * FLOW_GRID + fx))
            # Gate counting: is this track's foot inside an entrance/exit zone?
            if isinstance(pid, int) and resolved.gates:
                for _fid, pts in resolved.gates:
                    if point_in_polygon(px, py, pts):
                        fresh = (cam_path, pid) not in self._gate_seen
                        gate_hits.append((cam_path, pid, fresh))
                        break

        if not pending and not present_pids:
            return

        async with self._lock:
            for key, n in pending:
                self._buf[key] = self._buf.get(key, 0) + n
            # Count fresh gate crossings, refresh last-seen for all in-zone tracks.
            for cam, pid, fresh in gate_hits:
                self._gate_seen[(cam, pid)] = now
                if fresh:
                    vkey = (resolved.store_id, cam, hour)
                    self._visits[vkey] = self._visits.get(vkey, 0) + 1
            # Dwell presence: extend (or open) each visible track's span.
            for pid in present_pids:
                pk = (cam_path, pid)
                pres = self._presence.get(pk)
                if pres is None:
                    self._presence[pk] = _Presence(now, resolved.store_id, hour)
                else:
                    pres.last_mono = now
            # Movement flow: bump the edge when a track changes coarse cell.
            for pid, cell in track_cells:
                pres = self._presence.get((cam_path, pid))
                if pres is None:
                    continue
                prev = pres.last_flow_cell
                if prev is not None and prev != cell and _flow_adjacent(prev, cell):
                    ek = (resolved.store_id, cam_path, hour, prev, cell)
                    self._flow_buf[ek] = self._flow_buf.get(ek, 0) + 1
                pres.last_flow_cell = cell
            self._prune_gate_seen(now)
            self._prune_presence(now)
            due = (
                len(self._buf) >= _FLUSH_CELLS
                or (time.monotonic() - self._last_flush) >= _FLUSH_SECONDS
            )
            if due:
                await self._flush_locked()

    def _prune_presence(self, now: float) -> None:
        """Bank the dwell of tracks absent > _PRESENCE_TTL, then forget them.
        Runs opportunistically each frame; only touches genuinely-stale entries."""
        stale = [k for k, p in self._presence.items() if now - p.last_mono >= _PRESENCE_TTL]
        for k in stale:
            p = self._presence.pop(k)
            dwell_ms = int((p.last_mono - p.first_mono) * 1000)
            dkey = (p.store_id, k[0], p.hour)
            bucket = self._dwell_buf.get(dkey)
            if bucket is None:
                self._dwell_buf[dkey] = [1, dwell_ms]
            else:
                bucket[0] += 1
                bucket[1] += dwell_ms

    def _prune_gate_seen(self, now: float) -> None:
        """Forget tracks that left their gate zone > _GATE_TTL ago so a genuine
        re-entry recounts. Runs at most once per _GATE_TTL to stay cheap."""
        if now - self._gate_last_prune < _GATE_TTL:
            return
        self._gate_last_prune = now
        stale = [k for k, seen in self._gate_seen.items() if now - seen >= _GATE_TTL]
        for k in stale:
            del self._gate_seen[k]

    async def flush(self) -> None:
        """Force a flush (e.g. on shutdown)."""
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buf and not self._visits and not self._dwell_buf and not self._flow_buf:
            self._last_flush = time.monotonic()
            return
        # Group buffered cells by store for a single upsert per store.
        by_store: dict[UUID, list[BucketDelta]] = {}
        for (store_id, camera_id, hour_ts, gx, gy), n in self._buf.items():
            by_store.setdefault(store_id, []).append(BucketDelta(camera_id, hour_ts, gx, gy, n))
        visits_by_store: dict[UUID, list[tuple[str, datetime, int]]] = {}
        for (store_id, camera_id, hour_ts), n in self._visits.items():
            visits_by_store.setdefault(store_id, []).append((camera_id, hour_ts, n))
        dwell_by_store: dict[UUID, list[tuple[str, datetime, int, int]]] = {}
        for (store_id, camera_id, hour_ts), (tc, dms) in self._dwell_buf.items():
            dwell_by_store.setdefault(store_id, []).append((camera_id, hour_ts, tc, dms))
        flow_by_store: dict[UUID, list[tuple[str, datetime, int, int, int]]] = {}
        for (store_id, camera_id, hour_ts, fc, tc), n in self._flow_buf.items():
            flow_by_store.setdefault(store_id, []).append((camera_id, hour_ts, fc, tc, n))
        buf_snapshot = self._buf
        visits_snapshot = self._visits
        dwell_snapshot = self._dwell_buf
        flow_snapshot = self._flow_buf
        self._buf = {}
        self._visits = {}
        self._dwell_buf = {}
        self._flow_buf = {}
        self._last_flush = time.monotonic()
        try:
            async with session_scope() as db:
                for store_id, deltas in by_store.items():
                    org_id = self._store_org.get(store_id)
                    if org_id is None:
                        continue
                    await analytics_repo.bump_buckets(
                        db, organization_id=org_id, store_id=store_id, deltas=deltas
                    )
                for store_id, counts in visits_by_store.items():
                    org_id = self._store_org.get(store_id)
                    if org_id is None:
                        continue
                    await analytics_repo.bump_visits(
                        db, organization_id=org_id, store_id=store_id, counts=counts
                    )
                for store_id, dcounts in dwell_by_store.items():
                    org_id = self._store_org.get(store_id)
                    if org_id is None:
                        continue
                    await analytics_repo.bump_dwell(
                        db, organization_id=org_id, store_id=store_id, counts=dcounts
                    )
                for store_id, edges in flow_by_store.items():
                    org_id = self._store_org.get(store_id)
                    if org_id is None:
                        continue
                    await analytics_repo.bump_flow(
                        db, organization_id=org_id, store_id=store_id, edges=edges
                    )
                await db.commit()
        except Exception:  # noqa: BLE001
            # Put the counts back so a transient DB blip doesn't lose data.
            # Called with self._lock already held, so re-add directly.
            log.exception("footfall.flush_failed")
            for key, n in buf_snapshot.items():
                self._buf[key] = self._buf.get(key, 0) + n
            for vkey, n in visits_snapshot.items():
                self._visits[vkey] = self._visits.get(vkey, 0) + n
            for dkey, (tc, dms) in dwell_snapshot.items():
                b = self._dwell_buf.get(dkey)
                if b is None:
                    self._dwell_buf[dkey] = [tc, dms]
                else:
                    b[0] += tc
                    b[1] += dms
            for ekey, n in flow_snapshot.items():
                self._flow_buf[ekey] = self._flow_buf.get(ekey, 0) + n


def _hour_bucket(ts_ms: int) -> datetime:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC) if ts_ms > 0 else datetime.now(UTC)
    return dt.replace(minute=0, second=0, microsecond=0)


_aggregator: FootfallAggregator | None = None


def get_footfall_aggregator() -> FootfallAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = FootfallAggregator()
    return _aggregator
