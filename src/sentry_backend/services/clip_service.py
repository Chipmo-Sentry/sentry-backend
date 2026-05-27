"""Clip storage + hashing helpers."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile

from sentry_backend.settings import get_settings


async def save_upload_to_disk(
    upload: UploadFile,
    *,
    organization_id: UUID,
) -> tuple[Path, int, str]:
    """Stream upload to disk, return (path, size, sha256). Caller persists row.

    Layout: {clip_storage_dir}/{org_id}/{yyyy-mm}/{uuid4}.mp4
    """
    settings = get_settings()
    base = Path(settings.clip_storage_dir)
    now = datetime.now(UTC)
    subdir = base / str(organization_id) / now.strftime("%Y-%m")
    subdir.mkdir(parents=True, exist_ok=True)

    target = subdir / f"{uuid4()}.mp4"
    hasher = hashlib.sha256()
    size = 0

    with target.open("wb") as f:
        while chunk := await upload.read(1024 * 1024):
            f.write(chunk)
            hasher.update(chunk)
            size += len(chunk)

    return target, size, hasher.hexdigest()


def clip_size_within_limit(size_bytes: int) -> bool:
    settings = get_settings()
    return size_bytes <= settings.max_clip_size_mb * 1024 * 1024
