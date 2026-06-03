"""Behavior catalog validation + camera slug allocation (pure logic)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentry_backend.api.v1.behaviors import (
    BUILTIN_KEYS,
    BUILTIN_META,
    DEFAULT_THRESHOLDS,
    BehaviorConfigPatch,
    DimensionCreate,
    _seed_dimensions,
)
from sentry_backend.repository.camera_repo import _slugify


def test_builtin_seed_has_six_criteria() -> None:
    assert len(BUILTIN_META) == 6
    assert len(BUILTIN_KEYS) == 6


def test_seed_dimensions_are_builtin_and_active() -> None:
    for d in _seed_dimensions():
        assert d["builtin"] is True
        assert d["active"] is True
        assert d["weight"] >= 0


def test_default_thresholds_green_below_yellow() -> None:
    assert DEFAULT_THRESHOLDS["green_max"] < DEFAULT_THRESHOLDS["yellow_max"]


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
