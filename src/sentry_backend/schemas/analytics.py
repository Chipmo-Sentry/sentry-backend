"""Analytics response schemas (docs/30 F2)."""

from datetime import datetime

from pydantic import BaseModel, Field


class FootfallGrid(BaseModel):
    """Aggregated dwell heatmap for one store over a time window.

    `cells` are sparse — only non-zero cells are returned, each as [gx, gy, n].
    The frontend normalizes intensity by `max_samples` and lays cells out over
    the plan using `grid_size` and `size` (the plan's logical dimensions).
    """

    grid_size: int
    size: tuple[float, float]
    window_from: datetime
    window_to: datetime
    max_samples: int
    total_samples: int
    cells: list[tuple[int, int, int]] = Field(default_factory=list)


class TrafficPoint(BaseModel):
    """Visitors counted in one hour bucket."""

    hour: datetime
    entries: int


class TrafficSummary(BaseModel):
    """Visitor traffic over a window (docs/30 F3). `series` is one point per
    hour that had any traffic; `peak_hour` is the busiest of them (null if none)."""

    window_from: datetime
    window_to: datetime
    total: int
    peak_hour: datetime | None = None
    peak_entries: int = 0
    # Mean dwell time (seconds) across completed tracks; null if none yet.
    avg_dwell_seconds: float | None = None
    series: list[TrafficPoint] = Field(default_factory=list)


class ZoneActivity(BaseModel):
    """Footfall activity attributed to one plan fixture/zone (docs/30 F4)."""

    fixture_id: str
    type: str  # shelf | checkout | exit | entrance | furniture
    # Operator-given fixture name from the plan editor (falls back to the type
    # label in the UI when absent).
    label: str | None = None
    samples: int
    share: float  # 0-1 fraction of the store's total in-zone activity


class ZoneBreakdown(BaseModel):
    """Per-zone activity table for a store over a window, busiest first."""

    window_from: datetime
    window_to: datetime
    total_samples: int
    zones: list[ZoneActivity] = Field(default_factory=list)


class FlowEdge(BaseModel):
    """One movement edge between two coarse grid cells (docs/30 F4 flow).
    Coords are cell CENTRES normalized to the plan [0,1]² for direct drawing."""

    x1: float
    y1: float
    x2: float
    y2: float
    count: int


class FlowSummary(BaseModel):
    """Movement-flow graph for a store over a window."""

    grid_size: int
    window_from: datetime
    window_to: datetime
    max_count: int
    edges: list[FlowEdge] = Field(default_factory=list)


class ZoneFlowNode(BaseModel):
    """A semantic area of the store (a drawn fixture, or the walkway) — the
    node set of the zone-flow graph. Centroid normalized to the plan [0,1]²."""

    id: str
    label: str
    type: str  # shelf | fridge | mannequin | checkout | exit | walkway
    x: float
    y: float


class ZoneFlowEdge(BaseModel):
    """Net movement between two zones over the window (direction of the
    winner after opposite flows cancel)."""

    from_id: str
    to_id: str
    count: int


class ZoneFlowSummary(BaseModel):
    """Zone-to-zone movement graph (docs/30 F4 flow, zone level). The raw
    cell lattice reads as grid geometry, not behaviour — collapsing it onto
    the operator-drawn fixtures gives «Орц → Тавиур → Касс» arrows instead."""

    window_from: datetime
    window_to: datetime
    max_count: int
    nodes: list[ZoneFlowNode] = Field(default_factory=list)
    edges: list[ZoneFlowEdge] = Field(default_factory=list)


class WalkedPath(BaseModel):
    """One anonymous visitor's simplified walked polyline (docs/30 F4 paths),
    points normalized to the plan [0,1]²."""

    started_at: datetime
    duration_sec: float
    points: list[list[float]]
    gender: str | None = None  # male | female | None(unclassified)
    age_band: str | None = None  # child | youth | adult | senior | None


class PathsSummary(BaseModel):
    """Recent walked paths for the spaghetti layer."""

    window_from: datetime
    window_to: datetime
    paths: list[WalkedPath] = Field(default_factory=list)


class DemographicSlice(BaseModel):
    """One demographic bucket's share of the window (docs/30 F5)."""

    key: str  # gender: male|female|unknown; age: child|youth|adult|senior|unknown
    count: int
    share: float  # 0-1 fraction of classified visitors in the window


class DemographicsSummary(BaseModel):
    """Gender/age structure of classified visitors over a window (docs/30 F5).

    `total` counts CLASSIFIED tracks only — a store whose AI node runs no
    demographics model reports 0 and the frontend shows its own empty state.
    Slices are sorted by count, zero buckets omitted."""

    window_from: datetime
    window_to: datetime
    total: int
    gender: list[DemographicSlice] = Field(default_factory=list)
    age: list[DemographicSlice] = Field(default_factory=list)


class PeakCell(BaseModel):
    """Visitors in one (weekday, hour) slot. dow 1-7 = Mon-Sun (local time)."""

    dow: int
    hour: int
    entries: int


class PeakMatrix(BaseModel):
    """Weekday × hour visitor matrix (docs/30) — "when is the store busy". Sparse
    cells; the frontend lays them out on a 7×24 grid coloured by `max_entries`."""

    window_from: datetime
    window_to: datetime
    timezone: str
    max_entries: int
    cells: list[PeakCell] = Field(default_factory=list)


class RiskPoint(BaseModel):
    """One risk episode's peak-moment location on the plan (normalized 0-1)."""

    x: float
    y: float
    pct: float


class RiskEpisodeRow(BaseModel):
    """One finished risk episode for the analytics list."""

    ts: datetime
    camera_name: str
    peak_risk_pct: float
    level: str
    behaviors: list[str] = Field(default_factory=list)
    alerted: bool = False
    duration_sec: float = 0.0


class RiskCell(BaseModel):
    """Risk episodes in one (weekday, hour) slot. dow 1-7 = Mon-Sun (local)."""

    dow: int
    hour: int
    count: int


class RiskSummary(BaseModel):
    """Store risk analytics over a window: WHERE incidents cluster on the plan
    (points), WHEN they happen (weekday×hour cells), WHAT fires (top behaviors)
    and the most recent episodes — built from risk_episode activity-log rows the
    threshold handler has been writing all along."""

    window_from: datetime
    window_to: datetime
    timezone: str
    total: int
    alerted: int
    prev_total: int  # same-length window immediately before → trend arrow
    max_cell: int
    cells: list[RiskCell] = Field(default_factory=list)
    points: list[RiskPoint] = Field(default_factory=list)
    top_behaviors: list[DemographicSlice] = Field(default_factory=list)
    top_cameras: list[DemographicSlice] = Field(default_factory=list)
    recent: list[RiskEpisodeRow] = Field(default_factory=list)
