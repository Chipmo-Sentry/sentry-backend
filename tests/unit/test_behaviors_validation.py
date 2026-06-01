"""Behavior config PATCH validation + camera slug allocation (pure logic)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentry_backend.api.v1.behaviors import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    DIMENSION_KEYS,
    BehaviorConfigPatch,
)
from sentry_backend.repository.camera_repo import _slugify


def test_default_weights_cover_all_dimensions() -> None:
    assert set(DEFAULT_WEIGHTS.keys()) == set(DIMENSION_KEYS)


def test_default_thresholds_green_below_yellow() -> None:
    assert DEFAULT_THRESHOLDS["green_max"] < DEFAULT_THRESHOLDS["yellow_max"]


def test_patch_accepts_known_weight_keys() -> None:
    p = BehaviorConfigPatch(weights={"looking_around": 2.0, "crouch": 1.5})
    assert p.weights == {"looking_around": 2.0, "crouch": 1.5}


def test_patch_rejects_unknown_weight_key() -> None:
    with pytest.raises(ValidationError, match="unknown dimension"):
        BehaviorConfigPatch(weights={"nonexistent_dim": 1.0})


def test_patch_rejects_negative_weight() -> None:
    with pytest.raises(ValidationError, match="must be >= 0"):
        BehaviorConfigPatch(weights={"crouch": -1.0})


def test_patch_rejects_unknown_threshold_key() -> None:
    with pytest.raises(ValidationError, match="unknown threshold"):
        BehaviorConfigPatch(thresholds={"purple_max": 5.0})


def test_patch_rejects_negative_threshold() -> None:
    with pytest.raises(ValidationError, match="must be >= 0"):
        BehaviorConfigPatch(thresholds={"green_max": -5.0})


def test_patch_allows_empty() -> None:
    p = BehaviorConfigPatch()
    assert p.weights is None
    assert p.thresholds is None


# === Slug generator ===


def test_slugify_basic() -> None:
    assert _slugify("Hikvision DS-2CD") == "hikvision_ds_2cd"


def test_slugify_mongolian_falls_back() -> None:
    # All-Cyrillic name strips to empty → "cam"
    assert _slugify("Камер") == "cam"


def test_slugify_strips_leading_trailing_underscores() -> None:
    assert _slugify("  — test — ") == "test"


def test_slugify_truncates_long() -> None:
    out = _slugify("a" * 100)
    assert len(out) <= 50
