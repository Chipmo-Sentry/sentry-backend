"""Alert → Telegram notification — actionable gate + message formatting (no DB)."""

from __future__ import annotations

from sentry_backend.db.models.alert import (
    Alert,
    AlertCategory,
    AlertLevel,
    AlertTrigger,
)
from sentry_backend.services.alert_notify import _format, is_actionable


def test_only_notify_and_review_are_actionable() -> None:
    assert is_actionable(AlertLevel.notify)
    assert is_actionable(AlertLevel.review)
    assert not is_actionable(AlertLevel.log)
    assert not is_actionable(AlertLevel.ignore)


def _alert(**kw: object) -> Alert:
    defaults: dict[str, object] = {
        "category": AlertCategory.pocket_conceal,
        "confidence": 0.91,
        "reasoning": "Бараа халаасандаа хийсэн",
        "alert_level": AlertLevel.review,
        "triggered_by": AlertTrigger.live_threshold,
        "person_id": 7,
        "peak_risk_pct": 82.0,
    }
    defaults.update(kw)
    return Alert(**defaults)


def test_breach_message_includes_person_and_risk() -> None:
    msg = _format(_alert())
    assert "Шууд сэрэмжлүүлэг" in msg
    assert "Хүн #7" in msg
    assert "82" in msg
    assert "91%" in msg


def test_manual_upload_message_has_no_person_line() -> None:
    msg = _format(
        _alert(triggered_by=AlertTrigger.manual_upload, person_id=None, peak_risk_pct=None)
    )
    assert "Сэжигтэй үйлдэл" in msg
    assert "Хүн #" not in msg


def test_reasoning_is_truncated() -> None:
    msg = _format(_alert(reasoning="x" * 1000))
    # 300-char cap on the reasoning line keeps Telegram payloads sane.
    assert "x" * 300 in msg
    assert "x" * 301 not in msg
