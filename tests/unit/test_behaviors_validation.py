"""Behavior catalog validation + camera slug allocation (pure logic)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentry_backend.api.v1.behaviors import (
    BUILTIN_KEYS,
    BUILTIN_META,
    DEFAULT_ENGINE,
    DEFAULT_THRESHOLDS,
    BehaviorConfigPatch,
    DimensionCreate,
    DimensionUpdate,
    _reconcile_v2,
    _seed_dimensions,
)
from sentry_backend.repository.camera_repo import _slugify


def test_builtin_seed_v2_catalog() -> None:
    # v2 (ADR-0024): 14 criteria across 4 levels. docs/29 P1c made
    # exit_after_concealment + repeated_shelf_visit zone-aware detectors → 12
    # detector-backed + 2 inert multi-person placeholders (group_distraction,
    # coordinated_activity). rfid_mismatch dropped — needs RFID hardware.
    assert len(BUILTIN_META) == 14
    assert len(BUILTIN_KEYS) == 14
    detectors = [m for m in BUILTIN_META if m["has_detector"]]
    assert len(detectors) == 12
    # Every criterion carries a category + level 1-4.
    for m in BUILTIN_META:
        assert m["category"] in {"suspicious", "concealment", "organized", "critical"}
        assert 1 <= m["level"] <= 4


def test_seed_dimensions_active_matches_detector() -> None:
    for d in _seed_dimensions():
        assert d["builtin"] is True
        assert d["weight"] >= 0
        # Detector-backed criteria seed active; placeholders seed disabled.
        assert d["active"] == d["has_detector"]


def test_placeholders_seed_inactive() -> None:
    # Only the multi-person organized-theft criteria remain inert (no detector
    # yet); the zone-aware pair went live in docs/29 P1c.
    by_key = {d["key"]: d for d in _seed_dimensions()}
    for key in (
        "group_distraction",
        "coordinated_activity",
    ):
        assert by_key[key]["active"] is False
        assert by_key[key]["has_detector"] is False


def test_default_thresholds_ordered() -> None:
    g, y, h = (
        DEFAULT_THRESHOLDS["green_max"],
        DEFAULT_THRESHOLDS["yellow_max"],
        DEFAULT_THRESHOLDS["high_max"],
    )
    assert g < y < h


# === v2 migration of an existing (pre-v2) catalog row ===
def test_reconcile_upgrades_pre_v2_row() -> None:
    # Simulate the deployed v1 row: 6 criteria, raw-scale thresholds, no v2 fields.
    v1_dims = [
        {
            "key": "looking_around",
            "label_mn": "x",
            "description_mn": "",
            "weight": 1.5,
            "active": True,
            "builtin": True,
        },
        {
            "key": "item_pickup",
            "label_mn": "x",
            "description_mn": "",
            "weight": 15.0,
            "active": False,
            "builtin": True,
        },  # operator disabled this one
    ]
    dims, thresholds, _engine, changed = _reconcile_v2(
        v1_dims, {"green_max": 5.0, "yellow_max": 16.0}, {}
    )
    assert changed
    keys = {d["key"] for d in dims}
    assert BUILTIN_KEYS.issubset(keys)  # all 14 v2 criteria now present
    assert len(dims) == len(BUILTIN_META)
    # thresholds reset to v2 (scale changed) — high_max now present.
    assert thresholds == DEFAULT_THRESHOLDS
    # every dim has the v2 fields.
    for d in dims:
        assert "category" in d and "level" in d and "has_detector" in d
    # operator's disable toggle on item_pickup is preserved through the upgrade.
    assert next(d for d in dims if d["key"] == "item_pickup")["active"] is False


def test_reconcile_drops_retired_builtin() -> None:
    # An already-v2 store row that still carries a RETIRED built-in (rfid_mismatch,
    # now removed from meta) must have it pruned — otherwise it lingers in every
    # already-seeded store and still shows on the superadmin Behaviors page.
    dims = _seed_dimensions()  # current v2 catalog (14 built-ins)
    dims.append(
        {
            "key": "rfid_mismatch",
            "label_mn": "x",
            "description_mn": "",
            "weight": 100.0,
            "active": False,
            "builtin": True,
            "has_detector": False,
            "category": "critical",
            "level": 4,
            "params": {},
        }
    )
    out, _t, _e, changed = _reconcile_v2(dims, dict(DEFAULT_THRESHOLDS), {})
    keys = {d["key"] for d in out}
    assert "rfid_mismatch" not in keys
    assert keys == BUILTIN_KEYS  # exactly the current catalog, nothing extra
    assert changed is True


def test_reconcile_preserves_custom_criteria() -> None:
    v1_dims = [
        {
            "key": "looking_around",
            "label_mn": "x",
            "description_mn": "",
            "weight": 1.5,
            "active": True,
            "builtin": True,
        },
        {
            "key": "my_custom",
            "label_mn": "Custom",
            "description_mn": "",
            "weight": 4.0,
            "active": True,
            "builtin": False,
        },
    ]
    dims, _t, _e, _c = _reconcile_v2(v1_dims, {"green_max": 5.0, "yellow_max": 16.0}, {})
    custom = next(d for d in dims if d["key"] == "my_custom")
    assert custom["weight"] == 4.0 and custom["builtin"] is False


def test_reconcile_v2_row_is_idempotent() -> None:
    dims = _seed_dimensions()
    out, thresholds, _engine, changed = _reconcile_v2(
        dims, dict(DEFAULT_THRESHOLDS), dict(DEFAULT_ENGINE)
    )
    assert changed is False
    assert len(out) == len(dims)
    assert thresholds == DEFAULT_THRESHOLDS


# === Bulk patch (now accepts any key; applied to existing in the endpoint) ===
def test_patch_accepts_weight_keys() -> None:
    p = BehaviorConfigPatch(weights={"looking_around": 2.0, "custom_x": 1.5})
    assert p.weights == {"looking_around": 2.0, "custom_x": 1.5}


def test_patch_rejects_negative_weight() -> None:
    with pytest.raises(ValidationError, match="must be >= 0"):
        BehaviorConfigPatch(weights={"crouch": -1.0})


def test_patch_rejects_unknown_threshold_key() -> None:
    with pytest.raises(ValidationError, match="unknown threshold"):
        BehaviorConfigPatch(thresholds={"purple_max": 5.0})


def test_patch_allows_empty() -> None:
    p = BehaviorConfigPatch()
    assert p.weights is None and p.thresholds is None


# === Custom criterion key validation ===
def test_dimension_create_accepts_valid_key() -> None:
    d = DimensionCreate(key="loitering", label_mn="Удаан зогсох")
    assert d.weight == 1.0


@pytest.mark.parametrize("bad", ["A_bad", "1leading", "has space", "x", "has-dash"])
def test_dimension_create_rejects_bad_keys(bad: str) -> None:
    with pytest.raises(ValidationError):
        DimensionCreate(key=bad, label_mn="x")


# === Slug generator ===
def test_slugify_basic() -> None:
    assert _slugify("Hikvision DS-2CD") == "hikvision_ds_2cd"


def test_slugify_mongolian_falls_back() -> None:
    assert _slugify("Камер") == "cam"


def test_slugify_truncates_long() -> None:
    assert len(_slugify("a" * 100)) <= 50


# === Engine knobs + per-detector params (ADR-0024 v2 fine-tuning) ===
def test_seed_includes_detector_params() -> None:
    dims = _seed_dimensions()
    loiter = next(d for d in dims if d["key"] == "loitering")
    assert loiter["params"]["seconds"] == 30.0
    looking = next(d for d in dims if d["key"] == "looking_around")
    assert looking["params"]["offset_frac"] == 0.15


def test_reconcile_backfills_engine_and_params() -> None:
    # Simulate a v2 row seeded BEFORE per-detector params / engine shipped.
    dims = _seed_dimensions()
    for d in dims:
        d.pop("params", None)
    out, _t, engine, changed = _reconcile_v2(dims, dict(DEFAULT_THRESHOLDS), {})
    assert changed is True
    assert engine == DEFAULT_ENGINE
    assert next(d for d in out if d["key"] == "loitering")["params"]["seconds"] == 30.0


def test_engine_patch_validation() -> None:
    BehaviorConfigPatch(engine={"smooth_frames": 8, "decay_idle": 0.9})  # ok
    with pytest.raises(ValidationError, match="unknown engine knob"):
        BehaviorConfigPatch(engine={"bogus": 1.0})
    with pytest.raises(ValidationError, match=r"must be in \(0, 1\]"):
        BehaviorConfigPatch(engine={"decay_idle": 1.5})
    with pytest.raises(ValidationError, match="must be >= 0"):
        BehaviorConfigPatch(engine={"smooth_frames": -1.0})


def test_dimension_params_validation() -> None:
    DimensionUpdate(params={"seconds": 45.0})  # ok
    with pytest.raises(ValidationError, match="must be >= 0"):
        DimensionUpdate(params={"offset_frac": -0.1})
