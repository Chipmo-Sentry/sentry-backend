"""Clip storage + hashing helpers."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile

from sentry_backend.settings import get_settings


class ClipTooLargeError(Exception):
    """Upload exceeded max_clip_size_mb — raised mid-stream, partial file removed."""


async def save_upload_to_disk(
    upload: UploadFile,
    *,
    organization_id: UUID,
) -> tuple[Path, int, str]:
    """Stream upload to disk, return (path, size, sha256). Caller persists row.

    Layout: {clip_storage_dir}/{org_id}/{yyyy-mm}/{uuid4}.mp4

    Enforces the size cap WHILE streaming: once the running total exceeds
    max_clip_size_mb the partial file is deleted and ClipTooLargeError is raised,
    so an oversized upload can't fill the disk before rejection.
    """
    settings = get_settings()
    max_bytes = settings.max_clip_size_mb * 1024 * 1024
    base = Path(settings.clip_storage_dir)
    now = datetime.now(UTC)
    subdir = base / str(organization_id) / now.strftime("%Y-%m")
    subdir.mkdir(parents=True, exist_ok=True)

    target = subdir / f"{uuid4()}.mp4"
    hasher = hashlib.sha256()
    size = 0

    try:
        with target.open("wb") as f:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ClipTooLargeError
                f.write(chunk)
                hasher.update(chunk)
    except ClipTooLargeError:
        target.unlink(missing_ok=True)
        raise

    return target, size, hasher.hexdigest()


def clip_size_within_limit(size_bytes: int) -> bool:
    settings = get_settings()
    return size_bytes <= settings.max_clip_size_mb * 1024 * 1024
