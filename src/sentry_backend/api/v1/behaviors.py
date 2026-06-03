"""Behavior tuning — editable criteria catalog + global risk thresholds.

The catalog (criteria/dimensions) lives in the `app_config` row key='behavior'
as `{"dimensions": [...], "thresholds": {...}}`. Super-admins can add, rename,
weight, enable/disable, and delete custom criteria from the dashboard.

IMPORTANT — detection vs catalog: a criterion only contributes to the risk
score if sentry-ai has a coded *detector* for its key. The seeded built-in
criteria (`builtin=true`) have detectors; a custom criterion is inert (scores
0) until a detector for its key ships in sentry-ai. Disabling a criterion
(`active=false`) makes the poller send weight 0, so the scorer skips it.

Thresholds are ABSOLUTE accumulated-score units: green `< green_max`, yellow
`[green_max, yellow_max)`, red `>= yellow_max`. L5 fires when color == red.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.app_config import AppConfig
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db

router = APIRouter(prefix="/api/v1/behaviors", tags=["behaviors"])

BEHAVIOR_CONFIG_KEY = "behavior"
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# === Built-in criteria seed (have detectors in sentry-ai) ===
BUILTIN_META: list[dict[str, Any]] = [
    {
        "key": "looking_around",
        "label_mn": "Орчноо харах",
        "description_mn": "Нүүрний төв нь мөрний төвөөс хажуу тийш эргэсэн — санаатай эргэн харах дохио.",
        "weight": 1.5,
    },
    {
        "key": "item_pickup",
        "label_mn": "Бараа авах",
        "description_mn": "Бугуй нь COCO-аар илрүүлсэн барааны bbox дотор орсон — 'holding' төлвийг идэвхжүүлнэ.",
        "weight": 15.0,
    },
    {
        "key": "body_block",
        "label_mn": "Биеэр далдлах",
        "description_mn": "Мөрний өргөн rolling average-н 55%-аас бага — камер руу нуруугаа эргүүлж далдлах.",
        "weight": 3.0,
    },
    {
        "key": "crouch",
        "label_mn": "Бөхийх",
        "description_mn": "Бие/мөрнөөс хонго хүртэлх босоо урт хүний өндрийн 15%-аар агшсан — доогуур бараа авах/нуух.",
        "weight": 1.0,
    },
    {
        "key": "wrist_to_torso",
        "label_mn": "Хувцас доор нуух",
        "description_mn": "Бугуй хонгоны y-аас 15% дотор удаан үлдсэн — халаас/уут руу нуух. Бараа барьсан үед л идэвхжинэ.",
        "weight": 5.0,
    },
    {
        "key": "rapid_movement",
        "label_mn": "Хурдан хөдөлгөөн",
        "description_mn": "Бугуйн хурд хүний өндрийн 8%-аас илүү — нуух/эргэлзэх хурдан гар үйлдэл. Бараа барьсан үед л.",
        "weight": 1.5,
    },
]
BUILTIN_KEYS = {d["key"] for d in BUILTIN_META}

DEFAULT_THRESHOLDS: dict[str, float] = {"green_max": 5.0, "yellow_max": 15.0}


# === Schemas ===
class BehaviorDimension(BaseModel):
    key: str
    label_mn: str
    description_mn: str
    weight: float
    active: bool
    # builtin == has a coded detector in sentry-ai. `active_in_m1` kept for
    # frontend back-compat; equals `builtin`.
    active_in_m1: bool
    builtin: bool


class BehaviorConfig(BaseModel):
    dimensions: list[BehaviorDimension]
    thresholds: dict[str, float]
    color_labels: dict[str, str] = {
        "green": "Хэвийн",
        "yellow": "Анхаар",
        "red": "Сэжигтэй",
    }


class BehaviorConfigPatch(BaseModel):
    """Back-compat bulk update — weights (existing keys only) and/or thresholds."""

    weights: dict[str, float] | None = None
    thresholds: dict[str, float] | None = None

    @field_validator("weights")
    @classmethod
    def check_weights(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        for k, val in v.items():
            if val < 0:
                raise ValueError(f"weight for {k} must be >= 0")
        return v

    @field_validator("thresholds")
    @classmethod
    def check_thresholds(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        unknown = set(v.keys()) - {"green_max", "yellow_max"}
        if unknown:
            raise ValueError(f"unknown threshold(s): {sorted(unknown)}")
        for k, val in v.items():
            if val < 0:
                raise ValueError(f"{k} must be >= 0")
        return v


class DimensionCreate(BaseModel):
    key: str = Field(min_length=2, max_length=40)
    label_mn: str = Field(min_length=1, max_length=120)
    description_mn: str = Field(default="", max_length=1000)
    weight: float = Field(default=1.0, ge=0)

    @field_validator("key")
    @classmethod
    def check_key(cls, v: str) -> str:
        if not KEY_RE.match(v):
            raise ValueError("key must be lowercase letters/digits/underscore, start with a letter")
        return v


class DimensionUpdate(BaseModel):
    label_mn: str | None = Field(default=None, min_length=1, max_length=120)
    description_mn: str | None = Field(default=None, max_length=1000)
    weight: float | None = Field(default=None, ge=0)
    active: bool | None = None


# === Catalog persistence ===
def _seed_dimensions() -> list[dict[str, Any]]:
    return [
        {
            "key": m["key"],
            "label_mn": m["label_mn"],
            "description_mn": m["description_mn"],
            "weight": float(m["weight"]),
            "active": True,
            "builtin": True,
        }
        for m in BUILTIN_META
    ]


async def _load_catalog(
    db: AsyncSession,
) -> tuple[AppConfig, list[dict[str, Any]], dict[str, float]]:
    """Return (row, dimensions, thresholds). Seeds + migrates the DB row."""
    row = (
        await db.execute(select(AppConfig).where(AppConfig.key == BEHAVIOR_CONFIG_KEY))
    ).scalar_one_or_none()
    if row is None:
        row = AppConfig(
            key=BEHAVIOR_CONFIG_KEY,
            value={"dimensions": _seed_dimensions(), "thresholds": dict(DEFAULT_THRESHOLDS)},
        )
        db.add(row)
        await db.flush()
    value = dict(row.value)
    thresholds = {**DEFAULT_THRESHOLDS, **value.get("thresholds", {})}
    dims = value.get("dimensions")
    if not dims:
        # Migrate the legacy {weights, thresholds} shape into a catalog.
        old_weights = value.get("weights", {})
        dims = _seed_dimensions()
        for d in dims:
            if d["key"] in old_weights:
                d["weight"] = float(old_weights[d["key"]])
        row.value = {"dimensions": dims, "thresholds": thresholds}
        await db.flush()
    return row, dims, thresholds


def _to_response(dims: list[dict[str, Any]], thresholds: dict[str, float]) -> BehaviorConfig:
    return BehaviorConfig(
        dimensions=[
            BehaviorDimension(
                key=d["key"],
                label_mn=d["label_mn"],
                description_mn=d.get("description_mn", ""),
                weight=float(d["weight"]),
                active=bool(d.get("active", True)),
                active_in_m1=bool(d.get("builtin", False)),
                builtin=bool(d.get("builtin", False)),
            )
            for d in dims
        ],
        thresholds=thresholds,
    )


# === Endpoints ===
@router.get("", response_model=BehaviorConfig)
async def get_behavior_config(db: Annotated[AsyncSession, Depends(get_db)]) -> BehaviorConfig:
    _row, dims, thresholds = await _load_catalog(db)
    return _to_response(dims, thresholds)


@router.patch("", response_model=BehaviorConfig)
async def patch_behavior_config(
    body: BehaviorConfigPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    """Bulk weights (existing keys) + thresholds. Super-admin only."""
    row, dims, thresholds = await _load_catalog(db)
    if body.weights:
        for d in dims:
            if d["key"] in body.weights:
                d["weight"] = float(body.weights[d["key"]])
    if body.thresholds:
        new_t = {**thresholds, **body.thresholds}
        if new_t["green_max"] >= new_t["yellow_max"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="green_max must be < yellow_max",
            )
        thresholds = new_t
    row.value = {"dimensions": dims, "thresholds": thresholds}
    return _to_response(dims, thresholds)


@router.post("/dimensions", response_model=BehaviorConfig, status_code=status.HTTP_201_CREATED)
async def add_dimension(
    body: DimensionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    row, dims, thresholds = await _load_catalog(db)
    if any(d["key"] == body.key for d in dims):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Criterion '{body.key}' already exists",
        )
    dims.append(
        {
            "key": body.key,
            "label_mn": body.label_mn,
            "description_mn": body.description_mn,
            "weight": float(body.weight),
            "active": True,
            "builtin": False,
        }
    )
    row.value = {"dimensions": dims, "thresholds": thresholds}
    return _to_response(dims, thresholds)


@router.patch("/dimensions/{key}", response_model=BehaviorConfig)
async def update_dimension(
    key: str,
    body: DimensionUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    row, dims, thresholds = await _load_catalog(db)
    target = next((d for d in dims if d["key"] == key), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Criterion not found")
    if body.label_mn is not None:
        target["label_mn"] = body.label_mn
    if body.description_mn is not None:
        target["description_mn"] = body.description_mn
    if body.weight is not None:
        target["weight"] = float(body.weight)
    if body.active is not None:
        target["active"] = body.active
    row.value = {"dimensions": dims, "thresholds": thresholds}
    return _to_response(dims, thresholds)


@router.delete("/dimensions/{key}", response_model=BehaviorConfig)
async def delete_dimension(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    row, dims, thresholds = await _load_catalog(db)
    target = next((d for d in dims if d["key"] == key), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Criterion not found")
    if target.get("builtin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Built-in criteria can't be deleted (disable it instead)",
        )
    dims = [d for d in dims if d["key"] != key]
    row.value = {"dimensions": dims, "thresholds": thresholds}
    return _to_response(dims, thresholds)
