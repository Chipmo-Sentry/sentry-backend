"""Retention sweep — pure decision logic + file deletion (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sentry_backend.services.retention import (
    ClipAction,
    FileDeleteResult,
    RetentionSweeper,
    delete_clip_file,
    plan_clip_action,
    retention_cutoff,
)
from sentry_backend.settings import get_settings

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)


# --- retention_cutoff ------------------------------------------------------


def test_cutoff_is_now_minus_days() -> None:
    assert retention_cutoff(NOW, 90) == NOW - timedelta(days=90)


def test_zero_days_disables() -> None:
    assert retention_cutoff(NOW, 0) is None


def test_negative_days_disables() -> None:
    assert retention_cutoff(NOW, -7) is None


def test_one_day_boundary() -> None:
    cutoff = retention_cutoff(NOW, 1)
    assert cutoff is not None
    # A row created exactly at the cutoff is NOT deleted (sweep uses strict <).
    assert not (cutoff < cutoff)
    # A row one second older than the window is past the cutoff.
    assert NOW - timedelta(days=1, seconds=1) < cutoff


# --- plan_clip_action (alert-referenced rows must survive) ------------------


def test_unreferenced_clip_row_is_deleted() -> None:
    assert (
        plan_clip_action(has_alerts=False, file_result=FileDeleteResult.deleted)
        is ClipAction.delete_row
    )


def test_unreferenced_clip_with_missing_file_still_deletes_row() -> None:
    # File already gone (manual cleanup, lost volume) — row cleanup proceeds.
    assert (
        plan_clip_action(has_alerts=False, file_result=FileDeleteResult.missing)
        is ClipAction.delete_row
    )


def test_alert_referenced_clip_keeps_row() -> None:
    # alerts.clip_id is NOT NULL + ON DELETE CASCADE: deleting the row would
    # destroy the alert (business history). Only the file goes; row is marked.
    assert (
        plan_clip_action(has_alerts=True, file_result=FileDeleteResult.deleted)
        is ClipAction.keep_row_mark_deleted
    )
    assert (
        plan_clip_action(has_alerts=True, file_result=FileDeleteResult.missing)
        is ClipAction.keep_row_mark_deleted
    )


def test_unlink_error_retries_later_regardless_of_alerts() -> None:
    # File still on disk → keep the row (else the .mp4 is orphaned forever).
    for has_alerts in (True, False):
        assert (
            plan_clip_action(has_alerts=has_alerts, file_result=FileDeleteResult.error)
            is ClipAction.retry_later
        )


# --- delete_clip_file --------------------------------------------------------


def test_delete_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "evidence.mp4"
    f.write_bytes(b"x")
    assert delete_clip_file(str(f)) is FileDeleteResult.deleted
    assert not f.exists()


def test_delete_missing_file_is_skipped(tmp_path: Path) -> None:
    assert delete_clip_file(str(tmp_path / "gone.mp4")) is FileDeleteResult.missing


def test_delete_failure_reports_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "locked.mp4"
    f.write_bytes(b"x")

    def _boom(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", _boom)
    assert delete_clip_file(str(f)) is FileDeleteResult.error


# --- RetentionSweeper.start gating -------------------------------------------


@pytest.fixture()
def _settings_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


async def test_sweeper_disabled_when_interval_nonpositive(_settings_env) -> None:
    _settings_env.setenv("RETENTION_SWEEP_INTERVAL_SEC", "0")
    sweeper = RetentionSweeper()
    sweeper.start()
    assert sweeper._task is None  # noqa: SLF001 — asserting the loop never spawned
    await sweeper.stop()  # no-op, must not raise


async def test_sweeper_starts_and_stops(_settings_env) -> None:
    _settings_env.setenv("RETENTION_SWEEP_INTERVAL_SEC", "3600")
    # Disable every cleanup type so the loop's first sweep touches no DB.
    _settings_env.setenv("CLIP_RETENTION_DAYS", "0")
    _settings_env.setenv("METRIC_RETENTION_DAYS", "0")
    _settings_env.setenv("PAIRING_CODE_RETENTION_DAYS", "0")
    sweeper = RetentionSweeper()
    sweeper.start()
    assert sweeper._task is not None  # noqa: SLF001
    await sweeper.stop()
    assert sweeper._task is None  # noqa: SLF001


# --- sweep_once with everything disabled (no DB needed) ----------------------


async def test_sweep_once_all_disabled_touches_nothing(_settings_env) -> None:
    from sentry_backend.services.retention import sweep_once

    _settings_env.setenv("CLIP_RETENTION_DAYS", "0")
    _settings_env.setenv("METRIC_RETENTION_DAYS", "-1")
    _settings_env.setenv("PAIRING_CODE_RETENTION_DAYS", "0")
    summary = await sweep_once(now=NOW)
    assert all(v == 0 for v in summary.values())
