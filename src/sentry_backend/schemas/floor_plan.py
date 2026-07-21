"""Floor-plan schemas (docs/30) — the top-down store map authored in agent-pc.

The plan is per-store (Store.floor_plan JSONB). It is the SOURCE of truth; each
camera's `Camera.zones` are DERIVED from it on the agent via the camera's
homography, then PATCHed through the existing camera-CRUD (so the engine pipeline
is unchanged). Bounds mirror the zones caps — the agent is a semi-trusted client.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# `furniture` is scenery (буйдан/сандал/ширээ): drawable + shown in analytics,
# but the agent editor does NOT derive Camera.zones from it (no engine meaning).
# `fridge` is an item-taking area like `shelf` — it derives zones and feeds the
# repeated-visit behaviour.
# `sofa`/`chair` are named scenery like `furniture` (no zones). `door` is an
# INTERIOR doorway marker: not a visit gate (_extract_gates counts only
# entrance/exit), drawn as a wall opening on the plan.
FixtureType = Literal[
    "shelf",
    "exit",
    "entrance",
    "checkout",
    "furniture",
    "fridge",
    "mannequin",
    "sofa",
    "chair",
    "door",
]

# Plan units are METRES: the agent editor (v0.7.66+) fixes 1 plan-unit == 1 m,
# so `size` IS the store's real width × height. A typical retail store is
# ~10×10 m, so the default canvas is 20×20 (a 200×200 default made real stores
# render tiny). This matches the editor's DEFAULT_SIZE_M — keep the two in
# sync (assets/floorplan/app.js).
DEFAULT_PLAN_SIZE: tuple[float, float] = (20.0, 20.0)

# Defense-in-depth bounds (the agent is a semi-trusted client; cf. Camera.zones).
MAX_WALLS = 256
MAX_FIXTURES = 256
MAX_CAMERAS = 128
MAX_POLY_POINTS = 1024
MAX_CALIB_POINTS = 64
MAX_FIXTURE_LABEL = 64


class FloorWall(BaseModel):
    """A wall / fixture outline polyline in PLAN coordinates (not 0-1)."""

    points: list[tuple[float, float]]
    # Physical wall height in metres (agent 3D-calibration feature; default 2.8
    # on the agent side). Was silently stripped here before this field existed —
    # declared so it persists and the 3D plan view can extrude real heights.
    height_m: float | None = Field(default=None, ge=0, le=20)

    @field_validator("points")
    @classmethod
    def _polyline(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(v) < 2:
            raise ValueError("Хана дор хаяж 2 цэгтэй байх ёстой.")
        if len(v) > MAX_POLY_POINTS:
            raise ValueError(f"Хэт олон цэг (дээд тал {MAX_POLY_POINTS}).")
        return v


class FloorFixture(BaseModel):
    """A drawn zone on the plan (shelf/exit/entrance/checkout), PLAN coords."""

    id: str | None = None
    type: FixtureType
    # Optional operator-given name («Архины тавиур»…) — surfaces in /insights
    # zone analytics instead of the generic type label.
    label: str | None = Field(default=None, max_length=MAX_FIXTURE_LABEL)
    points: list[tuple[float, float]]
    # Physical fixture height in metres (per-type agent defaults: shelf 1.8,
    # fridge 2.0, checkout 1.0, mannequin 1.7, exit/furniture 0) — operator
    # overridable in the editor; drives the 3D plan view extrusion.
    height_m: float | None = Field(default=None, ge=0, le=20)

    @field_validator("points")
    @classmethod
    def _polygon(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(v) < 3:
            raise ValueError("Бүс дор хаяж 3 цэгтэй байх ёстой.")
        if len(v) > MAX_POLY_POINTS:
            raise ValueError(f"Хэт олон цэг (дээд тал {MAX_POLY_POINTS}).")
        return v


class CalibPoint(BaseModel):
    """One calibration pair: a NORMALIZED 0-1 camera-image point ↔ a plan point.

    NB: the field is `image` (not `img`) — the agent editor + `_compute_calibration`
    both emit/read `image`. A `img` field silently 422'd every calibration save
    (missing-required), so zones never persisted."""

    image: tuple[float, float]
    plan: tuple[float, float]


class FloorCamera(BaseModel):
    """A camera placed on the plan + its homography (set during calibration)."""

    camera_id: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=128)  # display label (round-trips)
    pos: tuple[float, float]
    dir_deg: float = 0.0
    # 3×3 homography: PLAN → normalized-image (0-1). None until calibrated.
    # (This is what cv2.findHomography(plan, img) yields in _compute_calibration;
    # consumers that need image→plan — e.g. the footfall aggregator — invert it.)
    homography: list[list[float]] | None = None
    # Radial lens term (v0.7.95+): H above is fitted against k1-UNDISTORTED
    # image coords — consumers must undistort observed points with this k1
    # before applying H⁻¹. None/0 = pre-k1 calibration, H works on raw coords.
    k1: float | None = None
    reproj_err: float | None = None
    calib_points: list[CalibPoint] | None = Field(default=None, max_length=MAX_CALIB_POINTS)

    @field_validator("homography")
    @classmethod
    def _shape(cls, v: list[list[float]] | None) -> list[list[float]] | None:
        if v is None:
            return v
        if len(v) != 3 or any(len(row) != 3 for row in v):
            raise ValueError("Homography 3×3 матриц байх ёстой.")
        return v


class FloorPlan(BaseModel):
    """The full per-store plan. Empty default = nothing drawn yet."""

    version: int = 1
    # Plan canvas size in METRES (1 unit == 1 m — see DEFAULT_PLAN_SIZE).
    size: tuple[float, float] = DEFAULT_PLAN_SIZE
    walls: list[FloorWall] = Field(default_factory=list, max_length=MAX_WALLS)
    fixtures: list[FloorFixture] = Field(default_factory=list, max_length=MAX_FIXTURES)
    cameras: list[FloorCamera] = Field(default_factory=list, max_length=MAX_CAMERAS)
