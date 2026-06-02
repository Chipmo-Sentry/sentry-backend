"""Behavior tuning — GET/PATCH global 6-dim weights + risk thresholds.

Stored as a single row in `app_config` table (key='behavior'). sentry-ai
live worker polls this endpoint and updates its in-memory scorer.

Thresholds are in ABSOLUTE accumulated-score units (not %): green band is
`score < green_max`, yellow is `[green_max, yellow_max)`, red is
`>= yellow_max`. The L5 threshold-breach handler fires when `color == red`.
"""

from __future__ import annotations

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

# === Dimension metadata (static — name/description/order; weights live in DB) ===

DIMENSION_META: list[dict[str, Any]] = [
    {
        "key": "looking_around",
        "label_mn": "Орчноо харах",
        "description_mn": (
            "Нүүрний төв нь мөрний төвөөс хажуу талд эргэсэн. "
            "Хүн санаачилгатай эргэн харж байгаа гэсэн дохио."
        ),
        "active_in_m1": True,
        "why_deferred": None,
    },
    {
        "key": "item_pickup",
        "label_mn": "Бараа авах",
        "description_mn": (
            "Бугуй нь COCO-аар илрүүлсэн (шил, гар утас, түрийвч, laptop, "
            "цүнх, ном гэх мэт) барааны bbox дотор орсон. Энэ нь тухайн "
            "хүний 'holding=True' төлвийг идэвхжүүлж дараагийн 2 шалгуурыг "
            "сэрээнэ."
        ),
        "active_in_m1": True,
        "why_deferred": None,
    },
    {
        "key": "body_block",
        "label_mn": "Биеэр далдлах",
        "description_mn": (
            "Мөрний өргөн нь rolling average-н 55%-аас бага. "
            "Хүн камер руу нуруугаа эргүүлж гар үйлдлийг далдалж байна."
        ),
        "active_in_m1": True,
        "why_deferred": None,
    },
    {
        "key": "crouch",
        "label_mn": "Бөхийх",
        "description_mn": (
            "Бие/мөрнөөс хонго хүртэлх босоо урт нь хүний өндрийн 15%-аас "
            "багасч агшсан. Тавиур доор бараа авах эсвэл нуух дохио."
        ),
        "active_in_m1": True,
        "why_deferred": None,
    },
    {
        "key": "wrist_to_torso",
        "label_mn": "Хувцас доор нуух",
        "description_mn": (
            "Бугуй хонгоны y-аас 15% дотор удаан хугацаагаар үлдсэн (8 frame "
            "тутамд оноо нэмэгдэнэ). Халаас/уут руу нуух дохио. Тухайн хүн "
            "бараа барьсан үед л идэвхжинэ."
        ),
        "active_in_m1": True,
        "why_deferred": None,
    },
    {
        "key": "rapid_movement",
        "label_mn": "Хурдан хөдөлгөөн",
        "description_mn": (
            "Бугуйн хурд нь хүний өндрийн 8%-аас илүү (frame тутмын зай). "
            "Бараа барьж байх үед эргэлзэх/нуух хурдан гар үйлдэл. Тухайн "
            "хүн бараа барьсан үед л идэвхжинэ."
        ),
        "active_in_m1": True,
        "why_deferred": None,
    },
]

DIMENSION_KEYS = [d["key"] for d in DIMENSION_META]

# === Default config — used when DB row missing on first request ===

DEFAULT_WEIGHTS: dict[str, float] = {
    "looking_around": 1.5,
    "item_pickup": 15.0,
    "body_block": 3.0,
    "crouch": 1.0,
    "wrist_to_torso": 5.0,
    "rapid_movement": 1.5,
}

# Absolute accumulated-score thresholds.
# score < green_max → green; < yellow_max → yellow; ≥ yellow_max → red.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "green_max": 5.0,
    "yellow_max": 15.0,
}


# === Schemas ===


class BehaviorDimension(BaseModel):
    key: str
    label_mn: str
    description_mn: str
    weight: float
    active_in_m1: bool
    why_deferred: str | None


class BehaviorConfig(BaseModel):
    dimensions: list[BehaviorDimension]
    thresholds: dict[str, float]
    # Color band textual labels — frontend convenience
    color_labels: dict[str, str] = {
        "green": "Хэвийн",
        "yellow": "Анхаар",
        "red": "Сэжигтэй",
    }


class BehaviorConfigPatch(BaseModel):
    """Partial update — any subset of {weights, thresholds}."""

    weights: dict[str, float] | None = Field(
        default=None,
        description="Map of dimension key → new weight. Unknown keys rejected.",
    )
    thresholds: dict[str, float] | None = Field(
        default=None,
        description="Subset of {green_max, yellow_max}. Both ≥ 0; green_max < yellow_max.",
    )

    @field_validator("weights")
    @classmethod
    def check_weight_keys(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        unknown = set(v.keys()) - set(DIMENSION_KEYS)
        if unknown:
            raise ValueError(f"unknown dimension(s): {sorted(unknown)}")
        for k, val in v.items():
            if val < 0:
                raise ValueError(f"weight for {k} must be >= 0")
        return v

    @field_validator("thresholds")
    @classmethod
    def check_thresholds(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        allowed = {"green_max", "yellow_max"}
        unknown = set(v.keys()) - allowed
        if unknown:
            raise ValueError(
                f"unknown threshold(s): {sorted(unknown)} (allowed: {sorted(allowed)})"
            )
        for k, val in v.items():
            if val < 0:
                raise ValueError(f"{k} must be >= 0")
        return v


# === Helpers ===


async def _load_config(db: AsyncSession) -> tuple[dict[str, float], dict[str, float]]:
    """Return (weights, thresholds). Lazily seeds DB row with defaults."""
    row = (
        await db.execute(select(AppConfig).where(AppConfig.key == BEHAVIOR_CONFIG_KEY))
    ).scalar_one_or_none()
    if row is None:
        row = AppConfig(
            key=BEHAVIOR_CONFIG_KEY,
            value={
                "weights": dict(DEFAULT_WEIGHTS),
                "thresholds": dict(DEFAULT_THRESHOLDS),
            },
        )
        db.add(row)
        await db.flush()
    weights = {**DEFAULT_WEIGHTS, **row.value.get("weights", {})}
    thresholds = {**DEFAULT_THRESHOLDS, **row.value.get("thresholds", {})}
    return weights, thresholds


def _to_response(weights: dict[str, float], thresholds: dict[str, float]) -> BehaviorConfig:
    dims = [
        BehaviorDimension(
            key=meta["key"],
            label_mn=meta["label_mn"],
            description_mn=meta["description_mn"],
            weight=float(weights.get(meta["key"], DEFAULT_WEIGHTS[meta["key"]])),
            active_in_m1=meta["active_in_m1"],
            why_deferred=meta["why_deferred"],
        )
        for meta in DIMENSION_META
    ]
    return BehaviorConfig(dimensions=dims, thresholds=thresholds)


# === Endpoints ===


@router.get("", response_model=BehaviorConfig)
async def get_behavior_config(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BehaviorConfig:
    """Current global behavior config. Public (any authenticated context can poll)."""
    weights, thresholds = await _load_config(db)
    return _to_response(weights, thresholds)


@router.patch("", response_model=BehaviorConfig)
async def patch_behavior_config(
    body: BehaviorConfigPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    """Update weights and/or thresholds. Super-admin only."""
    weights, thresholds = await _load_config(db)
    if body.weights:
        weights = {**weights, **body.weights}
    if body.thresholds:
        new_t = {**thresholds, **body.thresholds}
        if new_t["green_max"] >= new_t["yellow_max"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="green_max must be < yellow_max",
            )
        thresholds = new_t

    row = (
        await db.execute(select(AppConfig).where(AppConfig.key == BEHAVIOR_CONFIG_KEY))
    ).scalar_one()
    row.value = {"weights": weights, "thresholds": thresholds}

    return _to_response(weights, thresholds)
