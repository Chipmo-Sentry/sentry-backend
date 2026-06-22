"""Behavior tuning — editable criteria catalog + global risk thresholds.

The catalog (criteria/dimensions) lives in the `app_config` row key='behavior'
as `{"dimensions": [...], "thresholds": {...}}`. Super-admins can add, rename,
weight, enable/disable, and delete custom criteria from the dashboard.

IMPORTANT — detection vs catalog: a criterion only contributes to the risk
score if sentry-ai has a coded *detector* for its key. The seeded built-in
criteria (`builtin=true`) have detectors; a custom criterion is inert (scores
0) until a detector for its key ships in sentry-ai. Disabling a criterion
(`active=false`) makes the poller send weight 0, so the scorer skips it.

Thresholds are ABSOLUTE 0-100 risk-score units (ADR-0024 4-level model):
LOW `< green_max`, MEDIUM `[green_max, yellow_max)`, HIGH `[yellow_max,
high_max)`, CRITICAL `>= high_max`. L5 fires when a person's live risk crosses
the per-camera `risk_threshold` (sustained), then is VLM-confirmed.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from sentry_backend.db.models.app_config import AppConfig
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db

router = APIRouter(prefix="/api/v1/behaviors", tags=["behaviors"])

BEHAVIOR_CONFIG_KEY = "behavior"
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# === Behavior categories (ADR-0024 v2) ===
# category + level group the criteria for the UI; level ≈ severity tier.
CAT_SUSPICIOUS = "suspicious"  # LEVEL 1
CAT_CONCEALMENT = "concealment"  # LEVEL 2
CAT_ORGANIZED = "organized"  # LEVEL 3
CAT_CRITICAL = "critical"  # LEVEL 4

# === Built-in criteria seed (Behavior Engine v2, ADR-0024) ===
# has_detector=True → sentry-ai has coded logic for this key (scores live).
# has_detector=False → inert placeholder (shown in UI, scores 0) until a detector
#   ships — blocked on zones (shelf/exit), multi-person, or hardware (RFID).
BUILTIN_META: list[dict[str, Any]] = [
    # ── LEVEL 1: SUSPICIOUS ──────────────────────────────────────────────
    {
        "key": "looking_around",
        "label_mn": "Орчноо харах",
        "description_mn": "Хүн орчин, ажилтан, гарц, камерыг давтан ажиглах.",
        "weight": 2.0,
        "category": CAT_SUSPICIOUS,
        "level": 1,
        "has_detector": True,
    },
    {
        "key": "loitering",
        "label_mn": "Удаан зогсох",
        "description_mn": "Нэг тавиурын бүсэд тохируулсан хугацаанаас удаан зогсох (default 30 сек).",
        "weight": 3.0,
        "category": CAT_SUSPICIOUS,
        "level": 1,
        "has_detector": True,
    },
    {
        "key": "repeated_shelf_visit",
        "label_mn": "Тавиур давтан зочлох",
        "description_mn": "Нэг тавиур/бараанд олон удаа эргэж ирэх. ⚠ Бүс (shelf zone) шаардана — одоогоор идэвхгүй.",
        "weight": 3.0,
        "category": CAT_SUSPICIOUS,
        "level": 1,
        "has_detector": False,
    },
    {
        "key": "rapid_movement",
        "label_mn": "Хурдан хөдөлгөөн",
        "description_mn": "Бараа орчмын хэт хурдан гар/биеийн хөдөлгөөн. Бараа барьсан үед л.",
        "weight": 2.0,
        "category": CAT_SUSPICIOUS,
        "level": 1,
        "has_detector": True,
    },
    # ── LEVEL 2: CONCEALMENT ─────────────────────────────────────────────
    {
        "key": "item_pickup",
        "label_mn": "Бараа авах",
        "description_mn": "Бугуй COCO барааны bbox дотор — 'holding' төлөв идэвхжинэ (хэвийн худалдан авалт).",
        "weight": 10.0,
        "category": CAT_CONCEALMENT,
        "level": 2,
        "has_detector": True,
    },
    {
        "key": "wrist_to_torso",
        "label_mn": "Хувцас доор нуух",
        "description_mn": "Барьсан бараагаа бие/хувцас/халаас/уут руу ойртуулах. Гол нуун далдлалтын дохио.",
        "weight": 12.0,
        "category": CAT_CONCEALMENT,
        "level": 2,
        "has_detector": True,
    },
    {
        "key": "body_block",
        "label_mn": "Биеэр далдлах",
        "description_mn": "Барааны үйлдлийг камераас санаатай биеэрээ халхлах.",
        "weight": 5.0,
        "category": CAT_CONCEALMENT,
        "level": 2,
        "has_detector": True,
    },
    {
        "key": "crouch",
        "label_mn": "Бөхийх",
        "description_mn": "Бараа орчим бөхийж сууж бараатай харьцах.",
        "weight": 2.0,
        "category": CAT_CONCEALMENT,
        "level": 2,
        "has_detector": True,
    },
    {
        "key": "bag_interaction",
        "label_mn": "Гар уут руу",
        "description_mn": "Бараа барьсан үедээ уут нээх/гар уут руу хийх (handbag/backpack илрэлт).",
        "weight": 15.0,
        "category": CAT_CONCEALMENT,
        "level": 2,
        "has_detector": True,
    },
    {
        "key": "pocket_interaction",
        "label_mn": "Халаас руу",
        "description_mn": "Бараа барьсан үедээ гараа халаасны ойролцоо аваачих.",
        "weight": 12.0,
        "category": CAT_CONCEALMENT,
        "level": 2,
        "has_detector": True,
    },
    # ── LEVEL 3: ORGANIZED (multi-person — detector pending) ──────────────
    {
        "key": "group_distraction",
        "label_mn": "Бүлгийн сатаатуулалт",
        "description_mn": "Нэг нь ажилтныг сатааруулж, нөгөө нь бараа авах зохицуулсан үйлдэл. ⚠ Олон хүний шинжилгээ шаардана — идэвхгүй.",
        "weight": 20.0,
        "category": CAT_ORGANIZED,
        "level": 3,
        "has_detector": False,
    },
    {
        "key": "coordinated_activity",
        "label_mn": "Зохицуулсан үйлдэл",
        "description_mn": "Олон хүн нэг барааны бүсэд зохицуулан үйлдэх. ⚠ Олон хүний шинжилгээ шаардана — идэвхгүй.",
        "weight": 15.0,
        "category": CAT_ORGANIZED,
        "level": 3,
        "has_detector": False,
    },
    # ── LEVEL 4: CRITICAL ────────────────────────────────────────────────
    {
        "key": "concealment_sequence",
        "label_mn": "Нуун далдлах дараалал",
        "description_mn": "Бараа авах → хувцас доор → уут/халаас. Дараалал бүрэн болоход шууд ALERT.",
        "weight": 30.0,
        "category": CAT_CRITICAL,
        "level": 4,
        "has_detector": True,
    },
    {
        "key": "exit_after_concealment",
        "label_mn": "Нуусны дараа гарц руу",
        "description_mn": "Нуун далдлалтын дараа дэлгүүрийн гарц руу хөдлөх. ⚠ Гарцын бүс шаардана — идэвхгүй.",
        "weight": 50.0,
        "category": CAT_CRITICAL,
        "level": 4,
        "has_detector": False,
    },
]
BUILTIN_KEYS = {d["key"] for d in BUILTIN_META}

# Sequence rules (read-only, for the UI timeline) — mirrors sentry-ai
# behavior.DEFAULT_SEQUENCES. Bonus for `concealment_sequence` is tunable via its
# catalog weight; the rest are fixed engine rules.
SEQUENCE_META: list[dict[str, Any]] = [
    {"key": "seq_look_pickup", "pattern": ["looking_around", "item_pickup"], "bonus": 5.0},
    {"key": "seq_pickup_wrist", "pattern": ["item_pickup", "wrist_to_torso"], "bonus": 10.0},
    {"key": "seq_pickup_bag", "pattern": ["item_pickup", "bag_interaction"], "bonus": 15.0},
    {
        "key": "seq_pickup_wrist_bag",
        "pattern": ["item_pickup", "wrist_to_torso", "bag_interaction"],
        "bonus": 25.0,
    },
    {
        "key": "seq_loiter_pickup_conceal",
        "pattern": ["loitering", "item_pickup", "concealment"],
        "bonus": 15.0,
    },
    {
        "key": "concealment_sequence",
        "pattern": ["item_pickup", "wrist_to_torso", "bag/pocket"],
        "bonus": 30.0,
    },
]

# Risk levels (absolute 0-100, ADR-0024): LOW 0-10 / MEDIUM 11-25 / HIGH 26-50 /
# CRITICAL 51-100. green_max = LOW/MEDIUM cut, yellow_max = MEDIUM/HIGH cut,
# high_max = HIGH/CRITICAL cut.
DEFAULT_THRESHOLDS: dict[str, float] = {"green_max": 10.0, "yellow_max": 25.0, "high_max": 50.0}
_THRESHOLD_KEYS = {"green_max", "yellow_max", "high_max"}

# Engine globals — MIRROR sentry-ai `behavior.DEFAULT_ENGINE`. Hot-tuned: the
# config poller ships `engine` to the scorer's update_params(). Keep keys/defaults
# in sync with sentry-ai or a knob silently no-ops.
DEFAULT_ENGINE: dict[str, float] = {
    "smooth_frames": 3.0,  # consecutive frames a noisy dim must hold before it scores
    "decay_idle": 0.98,  # per-frame score decay when NOT holding an item
    "decay_holding": 0.999,  # per-frame decay while holding (slower → score persists)
    "sequence_window_sec": 60.0,  # window for an ordered pattern to complete
    "loiter_radius_frac": 0.25,  # dwell radius as a fraction of person height
    "stale_track_sec": 5.0,  # drop a per-track state unseen this long
}
_ENGINE_KEYS = set(DEFAULT_ENGINE)
# Engine knobs that are 0-1 multipliers (validated to (0, 1]); the rest are >= 0.
_ENGINE_UNIT_KEYS = {"decay_idle", "decay_holding"}

# Per-detector sensitivity params — MIRROR sentry-ai `behavior.DEFAULT_DETECTOR_PARAMS`.
# `*_frac` = fraction of person height; `cadence` = frame count; `seconds` = dwell time.
DEFAULT_DETECTOR_PARAMS: dict[str, dict[str, float]] = {
    "looking_around": {"offset_frac": 0.15},
    "body_block": {"collapse_frac": 0.55, "ema_alpha": 0.1},
    "crouch": {"frac": 0.15, "hold_floor": 5.0},
    "wrist_to_torso": {"frac": 0.15, "cadence": 8.0},
    "pocket_interaction": {"radius_frac": 0.12},
    "rapid_movement": {"frac": 0.08},
    "loitering": {"seconds": 30.0},
}


# === Schemas ===
class BehaviorDimension(BaseModel):
    key: str
    label_mn: str
    description_mn: str
    weight: float
    active: bool
    # v2 (ADR-0024): category + severity level for grouped UI display.
    category: str = CAT_SUSPICIOUS
    level: int = 1
    # has_detector == sentry-ai scores this key live. False → inert placeholder
    # (zones/multi-person/RFID pending). `active_in_m1`/`builtin` kept for
    # frontend back-compat; both equal has_detector for built-ins.
    has_detector: bool = True
    active_in_m1: bool
    builtin: bool
    # Per-detector tuning params (sensitivity fracs / seconds / cadence). Empty
    # for criteria with no tunable knobs (or custom criteria until a detector ships).
    params: dict[str, float] = {}


class SequenceInfo(BaseModel):
    key: str
    pattern: list[str]
    bonus: float


class BehaviorConfig(BaseModel):
    dimensions: list[BehaviorDimension]
    thresholds: dict[str, float]
    # Global engine knobs (smooth_frames, decay, sequence window, loiter radius…).
    engine: dict[str, float] = {}
    sequences: list[SequenceInfo] = []
    color_labels: dict[str, str] = {
        "green": "Хэвийн",
        "yellow": "Анхаар",
        "red": "Сэжигтэй",
    }
    level_labels: dict[str, str] = {
        "LOW": "Бага",
        "MEDIUM": "Дунд",
        "HIGH": "Өндөр",
        "CRITICAL": "Ноцтой",
    }


class BehaviorConfigPatch(BaseModel):
    """Bulk update — weights (existing keys only), thresholds, and/or engine knobs."""

    weights: dict[str, float] | None = None
    thresholds: dict[str, float] | None = None
    engine: dict[str, float] | None = None

    @field_validator("engine")
    @classmethod
    def check_engine(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        unknown = set(v.keys()) - _ENGINE_KEYS
        if unknown:
            raise ValueError(f"unknown engine knob(s): {sorted(unknown)}")
        for k, val in v.items():
            if k in _ENGINE_UNIT_KEYS:
                if not (0.0 < val <= 1.0):
                    raise ValueError(f"{k} must be in (0, 1]")
            elif val < 0:
                raise ValueError(f"{k} must be >= 0")
        return v

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
        unknown = set(v.keys()) - _THRESHOLD_KEYS
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
    category: str = Field(default=CAT_SUSPICIOUS)
    level: int = Field(default=1, ge=1, le=4)

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
    category: str | None = None
    level: int | None = Field(default=None, ge=1, le=4)
    # Merge-patch of this criterion's tuning params (e.g. {"seconds": 45} or
    # {"offset_frac": 0.22}). Only the supplied keys are updated; all must be >= 0.
    params: dict[str, float] | None = None

    @field_validator("params")
    @classmethod
    def check_params(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        for k, val in v.items():
            if val < 0:
                raise ValueError(f"param {k} must be >= 0")
        return v


# === Catalog persistence ===
def _seed_one(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": m["key"],
        "label_mn": m["label_mn"],
        "description_mn": m["description_mn"],
        "weight": float(m["weight"]),
        # Detector-backed criteria seed active; inert placeholders seed disabled.
        "active": bool(m["has_detector"]),
        "category": m["category"],
        "level": int(m["level"]),
        "has_detector": bool(m["has_detector"]),
        # Per-detector tuning params (sensitivity / seconds / cadence); {} if the
        # detector has no tunable knobs. Operator edits are preserved on reconcile.
        "params": dict(DEFAULT_DETECTOR_PARAMS.get(m["key"], {})),
        "builtin": True,
    }


def _seed_dimensions() -> list[dict[str, Any]]:
    return [_seed_one(m) for m in BUILTIN_META]


def _reconcile_v2(
    dims: list[dict[str, Any]], thresholds: dict[str, float], engine: dict[str, float]
) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, float], bool]:
    """Upgrade an existing catalog to the v2 (ADR-0024) shape in place.

    Existing deployments hold a pre-v2 `app_config['behavior']` row (the 6 v1
    criteria, raw-scale thresholds). Seeding only runs on a MISSING row, so
    without this those rows would never gain the new criteria/levels. Idempotent.

    - Pre-v2 row (no `high_max`, or built-ins missing `has_detector`): the score
      scale changed, so re-seed ALL built-ins from meta (resets weights to the v2
      anchors, adds the new criteria + placeholders) and reset thresholds to v2
      defaults. Operator-added CUSTOM criteria are preserved. The operator's
      enable/disable toggle on pre-existing built-ins is preserved.
    - Already-v2 row: additive only — append any built-in keys meta gained since,
      backfill `params` on built-ins seeded before per-detector tuning shipped,
      and fill any engine knob the row is missing. Operator edits untouched.
    """
    by_key = {d["key"]: d for d in dims}
    stored_builtins = [d for d in dims if d.get("builtin")]
    is_v2 = (
        "high_max" in thresholds
        and BUILTIN_KEYS.issubset(by_key.keys())
        and all("has_detector" in d for d in stored_builtins)
    )

    if is_v2:
        changed = False
        for m in BUILTIN_META:
            if m["key"] not in by_key:
                dims.append(_seed_one(m))
                changed = True
        # Additive: backfill per-detector params for built-ins seeded pre-tuning.
        for d in dims:
            if d.get("builtin") and "params" not in d:
                d["params"] = dict(DEFAULT_DETECTOR_PARAMS.get(d["key"], {}))
                changed = True
        # Additive: fill any missing engine knob (operator values preserved).
        for k, v in DEFAULT_ENGINE.items():
            if k not in engine:
                engine[k] = v
                changed = True
        return dims, thresholds, engine, changed

    # Pre-v2 → full upgrade. Preserve custom criteria + operator active toggles.
    custom = [d for d in dims if not d.get("builtin") and d["key"] not in BUILTIN_KEYS]
    new_builtins = _seed_dimensions()
    for nb in new_builtins:
        old = by_key.get(nb["key"])
        if old is not None and "active" in old:
            nb["active"] = bool(old["active"])
    return new_builtins + custom, dict(DEFAULT_THRESHOLDS), dict(DEFAULT_ENGINE), True


def _store(
    row: AppConfig,
    dims: list[dict[str, Any]],
    thresholds: dict[str, float],
    engine: dict[str, float],
) -> None:
    """Persist the catalog back to the app_config row.

    `value` is a plain JSONB column (not MutableDict). The loaded dims are shared
    by reference, so in-place edits (e.g. `target["active"] = ...`) also mutate
    SQLAlchemy's change-detection baseline — making the reassignment below look
    like a no-op (`new == old`) so NO UPDATE is emitted and the change is silently
    lost. `flag_modified` forces the column dirty so the write always persists.
    """
    row.value = {"dimensions": dims, "thresholds": thresholds, "engine": engine}
    flag_modified(row, "value")


async def _load_catalog(
    db: AsyncSession,
) -> tuple[AppConfig, list[dict[str, Any]], dict[str, float], dict[str, float]]:
    """Return (row, dimensions, thresholds, engine). Seeds + migrates the DB row."""
    row = (
        await db.execute(select(AppConfig).where(AppConfig.key == BEHAVIOR_CONFIG_KEY))
    ).scalar_one_or_none()
    if row is None:
        row = AppConfig(
            key=BEHAVIOR_CONFIG_KEY,
            value={
                "dimensions": _seed_dimensions(),
                "thresholds": dict(DEFAULT_THRESHOLDS),
                "engine": dict(DEFAULT_ENGINE),
            },
        )
        db.add(row)
        await db.flush()
        value = dict(row.value)
        return row, value["dimensions"], value["thresholds"], value["engine"]

    value = dict(row.value)
    thresholds = {**DEFAULT_THRESHOLDS, **value.get("thresholds", {})}
    engine = {**DEFAULT_ENGINE, **value.get("engine", {})}
    dims = value.get("dimensions")
    if not dims:
        # Migrate the legacy {weights, thresholds} shape into a catalog.
        old_weights = value.get("weights", {})
        dims = _seed_dimensions()
        for d in dims:
            if d["key"] in old_weights:
                d["weight"] = float(old_weights[d["key"]])

    # Reconcile to the v2 catalog (adds new criteria/levels/params to existing rows).
    dims, thresholds, engine, changed = _reconcile_v2(dims, thresholds, engine)
    if (
        changed
        or value.get("dimensions") != dims
        or value.get("thresholds") != thresholds
        or value.get("engine") != engine
    ):
        _store(row, dims, thresholds, engine)
        await db.flush()
    return row, dims, thresholds, engine


_BUILTIN_BY_KEY = {m["key"]: m for m in BUILTIN_META}


def _to_response(
    dims: list[dict[str, Any]], thresholds: dict[str, float], engine: dict[str, float]
) -> BehaviorConfig:
    out: list[BehaviorDimension] = []
    for d in dims:
        # Backfill v2 fields for rows seeded before ADR-0024 (built-ins from meta).
        meta = _BUILTIN_BY_KEY.get(d["key"], {})
        category = d.get("category") or meta.get("category", CAT_SUSPICIOUS)
        level = int(d.get("level") or meta.get("level", 1))
        has_detector = d.get("has_detector")
        if has_detector is None:
            has_detector = bool(meta.get("has_detector", d.get("builtin", False)))
        out.append(
            BehaviorDimension(
                key=d["key"],
                label_mn=d["label_mn"],
                description_mn=d.get("description_mn", ""),
                weight=float(d["weight"]),
                active=bool(d.get("active", True)),
                category=category,
                level=level,
                has_detector=bool(has_detector),
                active_in_m1=bool(has_detector),
                builtin=bool(d.get("builtin", False)),
                params={
                    k: float(v)
                    for k, v in (d.get("params") or {}).items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                },
            )
        )
    return BehaviorConfig(
        dimensions=out,
        thresholds=thresholds,
        engine={k: float(v) for k, v in engine.items()},
        sequences=[SequenceInfo(**s) for s in SEQUENCE_META],
    )


# === Endpoints ===
@router.get("", response_model=BehaviorConfig)
async def get_behavior_config(db: Annotated[AsyncSession, Depends(get_db)]) -> BehaviorConfig:
    _row, dims, thresholds, engine = await _load_catalog(db)
    return _to_response(dims, thresholds, engine)


@router.patch("", response_model=BehaviorConfig)
async def patch_behavior_config(
    body: BehaviorConfigPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    """Bulk weights (existing keys) + thresholds + engine knobs. Super-admin only."""
    row, dims, thresholds, engine = await _load_catalog(db)
    if body.weights:
        for d in dims:
            if d["key"] in body.weights:
                d["weight"] = float(body.weights[d["key"]])
    if body.thresholds:
        new_t = {**thresholds, **body.thresholds}
        if not (new_t["green_max"] < new_t["yellow_max"] < new_t.get("high_max", float("inf"))):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Босго утга green_max < yellow_max < high_max нөхцөлийг хангах ёстой.",
            )
        thresholds = new_t
    if body.engine:
        engine = {**engine, **{k: float(v) for k, v in body.engine.items()}}
    _store(row, dims, thresholds, engine)
    return _to_response(dims, thresholds, engine)


@router.post("/dimensions", response_model=BehaviorConfig, status_code=status.HTTP_201_CREATED)
async def add_dimension(
    body: DimensionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    row, dims, thresholds, engine = await _load_catalog(db)
    if any(d["key"] == body.key for d in dims):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{body.key}' шалгуур аль хэдийн байна.",
        )
    dims.append(
        {
            "key": body.key,
            "label_mn": body.label_mn,
            "description_mn": body.description_mn,
            "weight": float(body.weight),
            "active": True,
            "category": body.category,
            "level": body.level,
            # Custom criteria have no coded detector (or tunable params) until one
            # ships in sentry-ai.
            "has_detector": False,
            "params": {},
            "builtin": False,
        }
    )
    _store(row, dims, thresholds, engine)
    return _to_response(dims, thresholds, engine)


@router.patch("/dimensions/{key}", response_model=BehaviorConfig)
async def update_dimension(
    key: str,
    body: DimensionUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    row, dims, thresholds, engine = await _load_catalog(db)
    target = next((d for d in dims if d["key"] == key), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Шалгуур олдсонгүй.")
    if body.label_mn is not None:
        target["label_mn"] = body.label_mn
    if body.description_mn is not None:
        target["description_mn"] = body.description_mn
    if body.weight is not None:
        target["weight"] = float(body.weight)
    if body.active is not None:
        target["active"] = body.active
    if body.category is not None:
        target["category"] = body.category
    if body.level is not None:
        target["level"] = body.level
    if body.params is not None:
        merged = dict(target.get("params") or {})
        merged.update({k: float(v) for k, v in body.params.items()})
        target["params"] = merged
    _store(row, dims, thresholds, engine)
    return _to_response(dims, thresholds, engine)


@router.delete("/dimensions/{key}", response_model=BehaviorConfig)
async def delete_dimension(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_super_admin)],
) -> BehaviorConfig:
    row, dims, thresholds, engine = await _load_catalog(db)
    target = next((d for d in dims if d["key"] == key), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Шалгуур олдсонгүй.")
    if target.get("builtin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Үндсэн шалгуурыг устгах боломжгүй (идэвхгүй болгоно уу).",
        )
    dims = [d for d in dims if d["key"] != key]
    _store(row, dims, thresholds, engine)
    return _to_response(dims, thresholds, engine)
