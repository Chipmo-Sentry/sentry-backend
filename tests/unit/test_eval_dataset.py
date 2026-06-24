"""Unit tests for build_eval_dataset — feedback'd alerts → eval manifest entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentry_backend.api.v1.admin import build_eval_dataset

_T0 = datetime(2026, 6, 20, tzinfo=UTC)


def _row(aid, clip_id, cat, conf, verdict, t=_T0):  # type: ignore[no-untyped-def]
    return (aid, clip_id, cat, conf, verdict, t)


def test_maps_verdict_to_label_and_drops_unclear() -> None:
    rows = [
        _row("a", "clip-a", "pocket_conceal", 0.9, "true_positive"),
        _row("b", "clip-b", "browsing", 0.4, "false_positive"),
        _row("c", "clip-c", "other", 0.2, "unclear"),  # dropped
    ]
    out = build_eval_dataset(rows)
    by_path = {e["path"]: e for e in out}
    assert set(by_path) == {"clip-a", "clip-b"}  # unclear excluded
    assert by_path["clip-a"]["label"] == "theft"
    assert by_path["clip-a"]["predicted"] == "pocket_conceal"
    assert by_path["clip-a"]["confidence"] == 0.9
    assert by_path["clip-b"]["label"] == "benign"


def test_latest_verdict_per_alert_wins() -> None:
    rows = [
        _row("a", "clip-a", "pocket_conceal", 0.9, "true_positive", _T0),
        _row("a", "clip-a", "pocket_conceal", 0.9, "false_positive", _T0 + timedelta(hours=1)),
    ]
    out = build_eval_dataset(rows)
    assert len(out) == 1
    assert out[0]["label"] == "benign"  # the later verdict


def test_null_confidence_is_none() -> None:
    out = build_eval_dataset([_row("a", "clip-a", "other", None, "true_positive")])
    assert out[0]["confidence"] is None


def test_empty() -> None:
    assert build_eval_dataset([]) == []
