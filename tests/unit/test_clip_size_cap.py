"""Clip upload size cap is enforced MID-STREAM (disk-fill DoS guard)."""

from __future__ import annotations

import io
from uuid import uuid4

import pytest
from starlette.datastructures import UploadFile

from sentry_backend.services import clip_service
from sentry_backend.settings import get_settings


@pytest.fixture()
def _small_limit(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLIP_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_CLIP_SIZE_MB", "1")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_oversized_upload_raises_and_removes_partial(_small_limit) -> None:
    # 3 MB upload against a 1 MB cap.
    upload = UploadFile(filename="big.mp4", file=io.BytesIO(b"\0" * (3 * 1024 * 1024)))
    with pytest.raises(clip_service.ClipTooLargeError):
        await clip_service.save_upload_to_disk(upload, organization_id=uuid4())
    # No partial file left behind anywhere under the storage root.
    leftovers = list(_small_limit.rglob("*.mp4"))
    assert leftovers == [], leftovers


async def test_within_limit_upload_succeeds(_small_limit) -> None:
    upload = UploadFile(filename="ok.mp4", file=io.BytesIO(b"\0" * (512 * 1024)))
    path, size, sha = await clip_service.save_upload_to_disk(upload, organization_id=uuid4())
    assert size == 512 * 1024
    assert path.exists()
    assert len(sha) == 64
