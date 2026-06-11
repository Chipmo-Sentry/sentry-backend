"""Startup clip-storage check — must create the dir and scream when unwritable."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentry_backend.services.clip_service import ensure_clip_storage_writable
from sentry_backend.settings import get_settings


@pytest.fixture()
def _storage_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_creates_missing_dir_and_reports_writable(_storage_env, tmp_path: Path) -> None:
    target = tmp_path / "deep" / "clips"  # does not exist yet
    _storage_env.setenv("CLIP_STORAGE_DIR", str(target))
    assert ensure_clip_storage_writable() is True
    assert target.is_dir()
    # The probe file must not be left behind.
    assert list(target.iterdir()) == []


def test_unwritable_dir_logs_and_returns_false(_storage_env, tmp_path: Path) -> None:
    _storage_env.setenv("CLIP_STORAGE_DIR", str(tmp_path / "clips"))

    def _denied(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("read-only mount")

    _storage_env.setattr(Path, "mkdir", _denied)
    # Must NOT raise — the app should boot and log loudly instead.
    assert ensure_clip_storage_writable() is False
