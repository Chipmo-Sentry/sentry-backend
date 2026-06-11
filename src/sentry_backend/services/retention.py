"""Data-retention sweeper (T09/T13/T15 — the privacy policy's deletion promise).

The landing privacy policy promises footage is auto-deleted after a retention
window, but nothing enforced it: clips, telemetry and pairing codes accumulated
forever. This background loop (same shape as node_watchdog) runs every
`retention_sweep_interval_sec` and removes expired data:

* Clips (`clip_retention_days`, by row `created_at`): the on-disk .mp4 is
  deleted FIRST (an already-missing file is skipped with a log; an unlink
  error keeps the row for retry next sweep). Then the DB row is deleted —
  EXCEPT when an alert references the clip: `alerts.clip_id` is NOT NULL +
  ON DELETE CASCADE, so deleting such a row would silently destroy the alert,
  and alert rows are business history that must outlive the footage. Those
  clips keep their row as metadata with `file_deleted_at` stamped, which also
  keeps them out of the next sweep's scan.
* AI-node telemetry (`metric_retention_days`, by `ts`): deleted in batches of
  at most METRIC_BATCH rows per sweep — a backlog drains over several sweeps
  instead of one giant DELETE holding locks.
* Pairing codes (`pairing_code_retention_days`, by `expires_at`): both agent
  and ai-node codes. They expire within minutes anyway; rows are kept N days
  as a short audit trail, then dropped.

A `*_days` knob <= 0 disables that cleanup type (keep forever); interval <= 0
disables the whole loop. Each sweep logs a structured summary of what it
deleted; any error inside the loop is logged and the loop keeps running.

The decision helpers (`retention_cutoff`, `plan_clip_action`,
`delete_clip_file`) are pure / DB-free so unit tests exercise them directly.
"""

import asyncio
import enum
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.retention")

# Per-sweep caps: bound how much work (and how big a DELETE) one sweep does.
# A backlog larger than a cap simply drains over consecutive sweeps.
CLIP_BATCH = 200
METRIC_BATCH = 10_000
PAIRING_CODE_BATCH = 1_000


def _rowcount(result: object) -> int:
    """Rows affected by a DELETE. Narrowed via getattr: AsyncSession.execute is
    typed as the generic Result (no .rowcount), but DELETE statements really
    return a CursorResult that has it."""
    return int(getattr(result, "rowcount", 0) or 0)


def retention_cutoff(now: datetime, days: int) -> datetime | None:
    """Moment before which data is expired. None = retention disabled (<= 0)."""
    if days <= 0:
        return None
    return now - timedelta(days=days)


class FileDeleteResult(enum.Enum):
    deleted = "deleted"  # file existed and was removed
    missing = "missing"  # file already gone — row cleanup may proceed
    error = "error"  # unlink failed — keep the row, retry next sweep


class ClipAction(enum.Enum):
    delete_row = "delete_row"
    keep_row_mark_deleted = "keep_row_mark_deleted"  # alert references the clip
    retry_later = "retry_later"  # file unlink failed


def plan_clip_action(*, has_alerts: bool, file_result: FileDeleteResult) -> ClipAction:
    """Decide what happens to a clip ROW after the file pass.

    File first, row second: a row is only removed once its file is gone
    (deleted or already missing), so we can never leak an orphaned .mp4 with no
    DB record pointing at it. Rows referenced by alerts are NEVER deleted —
    alerts.clip_id is ON DELETE CASCADE and the alert is business history — the
    row is kept with `file_deleted_at` stamped instead.
    """
    if file_result is FileDeleteResult.error:
        return ClipAction.retry_later
    if has_alerts:
        return ClipAction.keep_row_mark_deleted
    return ClipAction.delete_row


def delete_clip_file(storage_path: str) -> FileDeleteResult:
    """Remove a clip file from disk. Missing is fine (already gone — log + skip)."""
    try:
        Path(storage_path).unlink()
    except FileNotFoundError:
        log.info("retention_clip_file_already_gone", path=storage_path)
        return FileDeleteResult.missing
    except OSError:
        log.warning("retention_clip_file_delete_failed", path=storage_path, exc_info=True)
        return FileDeleteResult.error
    return FileDeleteResult.deleted


async def sweep_once(now: datetime | None = None) -> dict[str, int]:
    """One full retention pass. Returns the per-type deletion counts (logged)."""
    # Imported here so the pure decision logic above stays importable without a
    # DB (unit tests exercise retention_cutoff/plan_clip_action/delete_clip_file
    # directly, mirroring node_watchdog).
    from sqlalchemy import delete, select

    from sentry_backend.db.models.agent import AgentPairingCode
    from sentry_backend.db.models.ai_node import AiNodeMetric, AiNodePairingCode
    from sentry_backend.db.models.alert import Alert
    from sentry_backend.db.models.clip import Clip
    from sentry_backend.db.session import session_scope

    settings = get_settings()
    if now is None:
        now = datetime.now(UTC)
    summary = {
        "clip_files_deleted": 0,
        "clip_rows_deleted": 0,
        "clip_rows_kept_for_alerts": 0,
        "clip_files_retry": 0,
        "metric_rows_deleted": 0,
        "pairing_codes_deleted": 0,
    }

    # --- (a) expired clips: file first, then row (alert-referenced rows stay) --
    clip_cutoff = retention_cutoff(now, settings.clip_retention_days)
    if clip_cutoff is not None:
        async with session_scope() as db:
            clips = (
                (
                    await db.execute(
                        select(Clip)
                        .where(Clip.created_at < clip_cutoff, Clip.file_deleted_at.is_(None))
                        .order_by(Clip.created_at)
                        .limit(CLIP_BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if clips:
                referenced = set(
                    (
                        await db.execute(
                            select(Alert.clip_id).where(Alert.clip_id.in_([c.id for c in clips]))
                        )
                    )
                    .scalars()
                    .all()
                )
                for clip in clips:
                    file_result = delete_clip_file(clip.storage_path)
                    if file_result is FileDeleteResult.deleted:
                        summary["clip_files_deleted"] += 1
                    action = plan_clip_action(
                        has_alerts=clip.id in referenced, file_result=file_result
                    )
                    if action is ClipAction.delete_row:
                        await db.delete(clip)
                        summary["clip_rows_deleted"] += 1
                    elif action is ClipAction.keep_row_mark_deleted:
                        clip.file_deleted_at = now
                        summary["clip_rows_kept_for_alerts"] += 1
                    else:
                        summary["clip_files_retry"] += 1

    # --- (b) old ai_node_metrics telemetry (bounded batch DELETE) -------------
    metric_cutoff = retention_cutoff(now, settings.metric_retention_days)
    if metric_cutoff is not None:
        async with session_scope() as db:
            expired_metric_ids = (
                select(AiNodeMetric.id).where(AiNodeMetric.ts < metric_cutoff).limit(METRIC_BATCH)
            )
            result = await db.execute(
                delete(AiNodeMetric).where(AiNodeMetric.id.in_(expired_metric_ids))
            )
            summary["metric_rows_deleted"] = _rowcount(result)

    # --- (c) long-expired pairing codes (agent + ai-node) ----------------------
    code_cutoff = retention_cutoff(now, settings.pairing_code_retention_days)
    if code_cutoff is not None:
        async with session_scope() as db:
            expired_agent_ids = (
                select(AgentPairingCode.id)
                .where(AgentPairingCode.expires_at < code_cutoff)
                .limit(PAIRING_CODE_BATCH)
            )
            res = await db.execute(
                delete(AgentPairingCode).where(AgentPairingCode.id.in_(expired_agent_ids))
            )
            summary["pairing_codes_deleted"] += _rowcount(res)

            expired_node_ids = (
                select(AiNodePairingCode.id)
                .where(AiNodePairingCode.expires_at < code_cutoff)
                .limit(PAIRING_CODE_BATCH)
            )
            res = await db.execute(
                delete(AiNodePairingCode).where(AiNodePairingCode.id.in_(expired_node_ids))
            )
            summary["pairing_codes_deleted"] += _rowcount(res)

    log.info("retention_sweep_done", **summary)
    return summary


class RetentionSweeper:
    """Background loop started/stopped from the app lifespan (like NodeWatchdog)."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        settings = get_settings()
        if settings.retention_sweep_interval_sec <= 0:
            log.info("retention_disabled")
            return
        self._task = asyncio.create_task(self._run(), name="retention-sweeper")
        log.info(
            "retention_started",
            interval_sec=settings.retention_sweep_interval_sec,
            clip_days=settings.clip_retention_days,
            metric_days=settings.metric_retention_days,
            pairing_code_days=settings.pairing_code_retention_days,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        interval = get_settings().retention_sweep_interval_sec
        while True:
            try:
                await sweep_once()
            except Exception:  # noqa: BLE001 — the loop must outlive any error
                log.warning("retention_sweep_failed", exc_info=True)
            await asyncio.sleep(interval)
