"""compute_clip_window — dynamic breach-clip sizing (whole-episode clips)."""

from __future__ import annotations

from sentry_backend.services.threshold_handler import (
    build_behavior_detail,
    compute_clip_window,
)


def test_build_behavior_detail_orders_and_looks_up() -> None:
    rows = build_behavior_detail(
        ["looking_around", "item_pickup", "concealment"],
        {"looking_around": 4.0, "item_pickup": 10.0, "concealment": 15.0},
        {"looking_around": 0.0, "item_pickup": 3.2, "concealment": 7.8},
    )
    assert rows == [
        {"key": "looking_around", "offset_sec": 0.0, "score": 4.0},
        {"key": "item_pickup", "offset_sec": 3.2, "score": 10.0},
        {"key": "concealment", "offset_sec": 7.8, "score": 15.0},
    ]


def test_build_behavior_detail_defaults_missing_and_empty() -> None:
    # A key present in the order but absent from the ledgers → 0.0, not a crash.
    assert build_behavior_detail(["x"], {}, {}) == [{"key": "x", "offset_sec": 0.0, "score": 0.0}]
    assert build_behavior_detail([], {"a": 1.0}, {"a": 1.0}) == []


_DEFAULTS = {"pre_pad_sec": 3, "post_roll_sec": 10.0, "max_sec": 90}


def test_short_episode() -> None:
    """Episode opened 20s before the breach → window covers it all."""
    breach = 1_700_000_020_000
    episode = 1_700_000_000_000  # 20s earlier
    start, dur = compute_clip_window(breach_ts_ms=breach, episode_started_ms=episode, **_DEFAULTS)
    # 20s episode + 3s pre-pad + 10s post-roll + 2s slack = 35s
    assert dur == 35
    assert start == -35


def test_instant_breach() -> None:
    """Episode opened at the breach frame — minimal window, like the old default."""
    ts = 1_700_000_000_000
    start, dur = compute_clip_window(breach_ts_ms=ts, episode_started_ms=ts, **_DEFAULTS)
    assert dur == 15  # 0 + 3 + 10 + 2
    assert start == -15


def test_long_episode_capped_keeps_breach_end() -> None:
    """A 5-minute loitering episode trims from the OLD end, never the breach."""
    breach = 1_700_000_300_000
    episode = 1_700_000_000_000  # 300s earlier
    start, dur = compute_clip_window(breach_ts_ms=breach, episode_started_ms=episode, **_DEFAULTS)
    assert dur == 90  # capped
    assert start == -90  # window still ends at cut time (breach + post-roll)


def test_unknown_episode_falls_back_to_legacy_preroll() -> None:
    start, dur = compute_clip_window(
        breach_ts_ms=1_700_000_000_000, episode_started_ms=None, **_DEFAULTS
    )
    assert dur == 17  # 5 legacy pre + 10 post + 2 slack
    assert start == -17


def test_clock_anomaly_episode_after_breach_ignored() -> None:
    """episode_started_ms newer than the breach frame (shouldn't happen) →
    treated as unknown rather than producing a negative pre-roll."""
    start, dur = compute_clip_window(
        breach_ts_ms=1_700_000_000_000,
        episode_started_ms=1_700_000_005_000,
        **_DEFAULTS,
    )
    assert dur == 17
    assert start == -17


def test_zero_post_roll() -> None:
    start, dur = compute_clip_window(
        breach_ts_ms=1_700_000_010_000,
        episode_started_ms=1_700_000_000_000,
        pre_pad_sec=3,
        post_roll_sec=0.0,
        max_sec=90,
    )
    assert dur == 15  # 10 + 3 + 0 + 2
    assert start == -15


def test_duration_never_exceeds_node_cap() -> None:
    """The node's CutVerifyRequest allows le=120 — our cap must stay within it."""
    _, dur = compute_clip_window(
        breach_ts_ms=2_000_000_000_000,
        episode_started_ms=1_000_000_000_000,  # absurdly long episode
        **_DEFAULTS,
    )
    assert dur <= 120
