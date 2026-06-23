"""Floor-plan schemas (docs/30) — the top-down store map authored in agent-pc.

The plan is per-store (Store.floor_plan JSONB). It is the SOURCE of truth; each
camera's `Camera.zones` are DERIVED from it on the agent via the camera's
homography, then PATCHed through the existing camera-CRUD (so the engine pipeline
is unchanged). Bounds mirror the zones caps — the agent is a semi-trusted client.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

FixtureType = Literal["shelf", "exit", "entrance", "checkout"]

# Defense-in-depth bounds (the agent is a semi-trusted client; cf. Camera.zones).
MAX_WALLS = 256
MAX_FIXTURES = 256
MAX_CAMERAS = 128
MAX_POLY_POINTS = 1024
MAX_CALIB_POINTS = 64


class FloorWall(BaseModel):
    """A wall / fixture outline polyline in PLAN coordinates (not 0-1)."""

    points: list[tuple[float, float]]

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
    points: list[tuple[float, float]]

    @field_validator("points")
    @classmethod
    def _polygon(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(v) < 3:
            raise ValueError("Бүс дор хаяж 3 цэгтэй байх ёстой.")
        if len(v) > MAX_POLY_POINTS:
            raise ValueError(f"Хэт олон цэг (дээд тал {MAX_POLY_POINTS}).")
        return v


class CalibPoint(BaseModel):
    """One calibration pair: a NORMALIZED 0-1 camera-image point ↔ a plan point."""

    img: tuple[float, float]
    plan: tuple[float, float]


class FloorCamera(BaseModel):
    """A camera placed on the plan + its homography (set during calibration)."""

    camera_id: str = Field(min_length=1, max_length=64)
    pos: tuple[float, float]
    dir_deg: float = 0.0
    # 3×3 homography: normalized-image (0-1) → plan coords. None until calibrated.
    homography: list[list[float]] | None = None
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
    # Plan canvas logical size (relative units; no real-world scale required).
    size: tuple[float, float] = (1000.0, 800.0)
    walls: list[FloorWall] = Field(default_factory=list, max_length=MAX_WALLS)
    fixtures: list[FloorFixture] = Field(default_factory=list, max_length=MAX_FIXTURES)
    cameras: list[FloorCamera] = Field(default_factory=list, max_length=MAX_CAMERAS)
