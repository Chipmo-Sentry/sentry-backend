"""Unit tests for the detection-quality aggregation (compute_quality_metrics) —
precision, labelled coverage, confidence calibration, alert-fatigue. Pure (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentry_backend.api.v1.admin import compute_quality_metrics

_T0 = datetime(2026, 6, 20, tzinfo=UTC)


def _row(aid, cat, conf, level, verdict, t=_T0):  # type: ignore[no-untyped-def]
    return (aid, cat, conf, level, verdict, t)


def test_precision_and_coverage() -> None:
    rows = [
        _row("a", "pocket_conceal", 0.9, "review", "true_positive"),
        _row("b", "pocket_conceal", 0.8, "review", "false_positive"),
        _row("c", "bag_conceal", 0.75, "notify", "true_positive"),
        _row("d", "browsing", 0.4, "log", "unclear"),
    ]
    m = compute_quality_metrics("30d", total_alerts=10, feedback_rows=rows, days=30.0)
    assert m["tp"] == 2 and m["fp"] == 1 and m["unclear"] == 1
    assert m["precision"] == round(2 / 3, 3)  # unclear excluded
    assert m["labeled"] == 4
    assert m["coverage"] == round(4 / 10, 3)


def test_latest_verdict_per_alert_wins() -> None:
    # Same alert reviewed twice: the later verdict (FP) replaces the earlier (TP).
    rows = [
        _row("a", "pocket_conceal", 0.9, "review", "true_positive", _T0),
        _row("a", "pocket_conceal", 0.9, "review", "false_positive", _T0 + timedelta(hours=1)),
    ]
    m = compute_quality_metrics("30d", total_alerts=1, feedback_rows=rows, days=30.0)
    assert m["labeled"] == 1
    assert m["tp"] == 0 and m["fp"] == 1


def test_false_alerts_per_day_counts_only_staff_levels() -> None:
    # FP at notify/review drives fatigue; an FP that only got logged does not.
    rows = [
        _row("a", "pocket_conceal", 0.9, "review", "false_positive"),
        _row("b", "bag_conceal", 0.8, "notify", "false_positive"),
        _row("c", "browsing", 0.2, "log", "false_positive"),  # not shown to staff
        _row("d", "ignore_cat", 0.1, "ignore", "false_positive"),
    ]
    m = compute_quality_metrics("7d", total_alerts=4, feedback_rows=rows, days=7.0)
    # 2 staff-level FPs over 7 days
    assert m["false_alerts_per_day"] == round(2 / 7, 2)


def test_confidence_calibration_buckets() -> None:
    rows = [
        _row("a", "x", 0.95, "review", "true_positive"),  # 0.85–1.00
        _row("b", "x", 0.90, "review", "true_positive"),  # 0.85–1.00
        _row("c", "x", 0.90, "review", "false_positive"),  # 0.85–1.00
        _row("d", "x", 0.60, "notify", "false_positive"),  # 0.50–0.70
    ]
    m = compute_quality_metrics("30d", total_alerts=4, feedback_rows=rows, days=30.0)
    by_b = {b["bucket"]: b for b in m["by_confidence"]}  # type: ignore[index]
    assert by_b["0.85–1.00"]["tp"] == 2 and by_b["0.85–1.00"]["fp"] == 1
    assert by_b["0.85–1.00"]["tp_rate"] == round(2 / 3, 3)
    assert by_b["0.50–0.70"]["tp_rate"] == 0.0  # 0 TP / 1 FP


def test_per_category_precision() -> None:
    rows = [
        _row("a", "pocket_conceal", 0.9, "review", "true_positive"),
        _row("b", "pocket_conceal", 0.9, "review", "true_positive"),
        _row("c", "browsing", 0.4, "log", "false_positive"),
    ]
    m = compute_quality_metrics("30d", total_alerts=3, feedback_rows=rows, days=30.0)
    by_cat = {c["category"]: c for c in m["by_category"]}  # type: ignore[index]
    assert by_cat["pocket_conceal"]["precision"] == 1.0
    assert by_cat["browsing"]["precision"] == 0.0


def test_empty_is_safe() -> None:
    m = compute_quality_metrics("30d", total_alerts=0, feedback_rows=[], days=30.0)
    assert m["precision"] is None
    assert m["coverage"] == 0.0
    assert m["labeled"] == 0
    assert m["false_alerts_per_day"] == 0.0
