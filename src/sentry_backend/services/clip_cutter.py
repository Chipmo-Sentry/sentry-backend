"""Cut a time window out of MediaMTX-recorded fmp4 segments.

M1 simplification: backend has filesystem access to sentry-ingest's recordings/
directory (same machine). For M2+ remote pilot this moves into a thin Python
control plane co-located with MediaMTX on Hetzner.

Approach:
  1. Scan <mediamtx_recordings_dir>/<camera_path>/ for segment files
  2. Parse "YYYY-MM-DD_HH-MM-SS-µs" timestamp from each filename
  3. Concatenate segments overlapping [target_start, target_start+duration]
  4. ffmpeg -ss/-t to trim to the exact window
  5. Write to <clip_storage_dir>/live_<ts>_<cam>.mp4, return path + metadata
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.clip_cutter")

# MediaMTX fmp4 segment filenames: "2026-05-28_14-58-34-265139.mp4"
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?)")


@dataclass(slots=True)
class CutResult:
    storage_path: str
    file_size_bytes: int
    duration_sec: float
    captured_at: datetime


class ClipCutError(RuntimeError):
    pass


def _parse_segment_ts(filename: str) -> datetime | None:
    """Parse MediaMTX segment filename "YYYY-MM-DD_HH-MM-SS-µs.mp4".

    The `_` separates date and time; both halves use `-`.
    """
    m = _TS_RE.match(filename)
    if not m:
        return None
    s = m.group(1)
    if "_" not in s:
        return None
    date_str, time_str_full = s.split("_", 1)
    time_parts = time_str_full.split("-")
    if len(time_parts) < 3:
        return None
    try:
        hour, minute, sec = time_parts[0], time_parts[1], time_parts[2]
        dt = datetime.fromisoformat(f"{date_str}T{hour}:{minute}:{sec}")
        if len(time_parts) >= 4:
            try:
                micros = int(time_parts[3])
                dt = dt + timedelta(microseconds=micros)
            except ValueError:
                pass
        return dt
    except (ValueError, IndexError):
        return None


def _find_segments_in_window(
    cam_dir: Path,
    window_start: datetime,
    window_end: datetime,
) -> list[Path]:
    """List segments that overlap [window_start, window_end]. Sorted by ts."""
    if not cam_dir.is_dir():
        return []
    segments: list[tuple[datetime, Path]] = []
    for p in cam_dir.iterdir():
        if not p.is_file() or p.suffix != ".mp4":
            continue
        # Skip our own cut output files if they accidentally end up here
        if p.name.startswith("live_"):
            continue
        ts = _parse_segment_ts(p.name)
        if ts is None:
            continue
        segments.append((ts, p))
    segments.sort(key=lambda x: x[0])

    # A segment overlaps the window if it starts before window_end AND
    # the next segment's start (or 'forever') is after window_start.
    overlapping: list[Path] = []
    for i, (ts, path) in enumerate(segments):
        next_ts = segments[i + 1][0] if i + 1 < len(segments) else window_end + timedelta(hours=1)
        if ts < window_end and next_ts > window_start:
            overlapping.append(path)
    return overlapping


async def _run_ffmpeg(args: list[str], timeout: float = 30.0) -> None:  # noqa: ASYNC109
    """Run ffmpeg with strict error reporting."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        raise ClipCutError(f"ffmpeg timed out after {timeout}s") from e
    if proc.returncode != 0:
        tail = stderr_bytes.decode("utf-8", errors="replace")[-500:]
        raise ClipCutError(f"ffmpeg exit {proc.returncode}: {tail}")


async def cut_window(
    camera_mediamtx_path: str,
    start_offset_sec: int = -5,
    duration_sec: int = 15,
) -> CutResult:
    """Cut a [now+start_offset, now+start_offset+duration] window from recordings.

    `start_offset_sec` is typically negative (pre-event roll-back).
    """
    settings = get_settings()
    if not settings.mediamtx_recordings_dir:
        raise ClipCutError("MEDIAMTX_RECORDINGS_DIR not configured")

    recordings_root = Path(settings.mediamtx_recordings_dir)
    cam_dir = recordings_root / camera_mediamtx_path
    if not cam_dir.is_dir():
        raise ClipCutError(f"camera dir not found: {cam_dir}")

    # MediaMTX writes filenames in local time → match in local time too
    now = datetime.now()
    window_start = now + timedelta(seconds=start_offset_sec)
    window_end = window_start + timedelta(seconds=duration_sec)
    capture_ts_utc = now.astimezone(UTC).replace(microsecond=0) + timedelta(
        seconds=start_offset_sec,
    )

    segments = _find_segments_in_window(cam_dir, window_start, window_end)
    if not segments:
        raise ClipCutError(
            f"no segments overlap window [{window_start}, {window_end}] in {cam_dir}",
        )

    # Output path. Resolve to absolute so downstream services (sentry-ai
    # in a different CWD) can open the file. Tiny sync I/O.
    out_dir = Path(settings.clip_storage_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    out_name = f"live_{capture_ts_utc:%Y%m%dT%H%M%SZ}_{camera_mediamtx_path}_{uuid4().hex[:8]}.mp4"
    out_path = out_dir / out_name

    # We need to figure out how far into the FIRST segment the window starts,
    # because we'll concat-demux and then -ss/-t to trim.
    first_seg_ts = _parse_segment_ts(segments[0].name)
    if first_seg_ts is None:
        raise ClipCutError("could not parse first segment timestamp")
    seek_into_first = max(0.0, (window_start - first_seg_ts).total_seconds())

    # ffmpeg concat demuxer requires a list file with "file 'abs/path'" lines
    list_path = out_dir / f"_concat_{uuid4().hex[:8]}.txt"
    try:
        list_path.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in segments),
            encoding="utf-8",
        )

        # Two-step: copy-mux concat to TMP, then trim. Re-encode on trim to
        # guarantee a keyframe at offset 0 (so the saved clip plays cleanly
        # in browsers without "blank for 2 sec" startup).
        ffmpeg_args = [
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", list_path.as_posix(),
            "-ss", f"{seek_into_first:.3f}",
            "-t", f"{duration_sec}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            out_path.as_posix(),
        ]
        await _run_ffmpeg(ffmpeg_args, timeout=60.0)
    finally:
        list_path.unlink(missing_ok=True)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise ClipCutError("ffmpeg produced empty output")

    log.info(
        "clip_cutter.ok",
        camera_path=camera_mediamtx_path,
        out=out_path.name,
        size=out_path.stat().st_size,
        segments=len(segments),
    )
    return CutResult(
        # as_posix() → forward-slash absolute path; safer across OS + log readers
        storage_path=out_path.resolve().as_posix(),
        file_size_bytes=out_path.stat().st_size,
        duration_sec=float(duration_sec),
        captured_at=capture_ts_utc,
    )
