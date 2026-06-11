"""Clip storage + hashing helpers."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.clip_service")


class ClipTooLargeError(Exception):
    """Upload exceeded max_clip_size_mb — raised mid-stream, partial file removed."""


def ensure_clip_storage_writable() -> bool:
    """Create clip_storage_dir if missing and PROVE it is writable (T09).

    Called at startup so a broken evidence store screams in the boot logs
    instead of failing silently at the first upload/threshold-cut. The previous
    behavior was exactly that silent failure: nothing checked the directory
    until a clip write 500'd. Never raises — logs a loud, actionable error and
    returns False so the app still boots (live view etc. keep working).
    """
    base = Path(get_settings().clip_storage_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / f".write-probe-{uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        log.error(
            "clip_storage_not_writable",
            dir=str(base),
            hint=(
                "Evidence clips CANNOT be saved. On Railway: attach a Volume "
                "(mount path e.g. /data) and set CLIP_STORAGE_DIR=/data/clips; "
                "the container runs as non-root user 'app', so make sure the "
                "volume mount is writable by it (see railway.toml notes)."
            ),
            exc_info=True,
        )
        return False
    log.info("clip_storage_ready", dir=str(base.resolve()))
    return True


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
    # Resolved from settings.clip_storage_dir (env CLIP_STORAGE_DIR). In prod
    # this is the Railway Volume mount (/data/clips) so clips survive restarts;
    # the dir tree is created on demand below.
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
