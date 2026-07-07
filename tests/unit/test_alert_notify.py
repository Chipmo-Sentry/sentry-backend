"""Alert → Telegram notification — actionable gate + message formatting + the
evidence-clip resolver that decides text-vs-video (no network)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sentry_backend.db.models.alert import (
    Alert,
    AlertCategory,
    AlertLevel,
    AlertTrigger,
)
from sentry_backend.services.alert_notify import (
    _TELEGRAM_VIDEO_MAX_BYTES,
    _format,
    _resolve_clip_path,
    is_actionable,
)


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


def test_edge_pc_upload_message_is_suspicion_not_breach() -> None:
    # ADR-0029 I2: an edge clip alert is a suspicion (🔔), not a live breach (🚨),
    # and has no person/risk line (the edge agent doesn't send a person id).
    msg = _format(
        _alert(triggered_by=AlertTrigger.edge_pc_upload, person_id=None, peak_risk_pct=None)
    )
    assert "Сэжигтэй үйлдэл" in msg
    assert "Шууд сэрэмжлүүлэг" not in msg
    assert "Хүн #" not in msg


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeDB:
    """Minimal AsyncSession stub — execute() returns a fixed scalar (the clip's
    storage_path) so the resolver can run without a real database."""

    def __init__(self, storage_path: object) -> None:
        self._storage_path = storage_path

    async def execute(self, *_a: object, **_k: object) -> _ScalarResult:
        return _ScalarResult(self._storage_path)


@pytest.mark.asyncio
async def test_resolve_clip_path_returns_existing_small_file(tmp_path: Path) -> None:
    clip = tmp_path / "evidence.mp4"
    clip.write_bytes(b"\x00" * 1024)
    alert = _alert(clip_id=uuid4())
    resolved = await _resolve_clip_path(_FakeDB(str(clip)), alert)  # type: ignore[arg-type]
    assert resolved == clip


@pytest.mark.asyncio
async def test_resolve_clip_path_none_when_missing_or_oversized(tmp_path: Path) -> None:
    alert = _alert(clip_id=uuid4())
    # Path in DB but no file on disk → text-only fallback.
    assert await _resolve_clip_path(_FakeDB(str(tmp_path / "gone.mp4")), alert) is None  # type: ignore[arg-type]
    # DB has no storage_path row at all.
    assert await _resolve_clip_path(_FakeDB(None), alert) is None  # type: ignore[arg-type]
    # Oversized clip → skip the video, send text.
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\x00" * (_TELEGRAM_VIDEO_MAX_BYTES + 1))
    assert await _resolve_clip_path(_FakeDB(str(big)), alert) is None  # type: ignore[arg-type]
