"""Organization deletion — the privacy policy's "delete your account" promise (T15 #3).

Shared by the two delete endpoints (super-admin `DELETE /api/v1/admin/orgs/{id}`
and owner self-service `DELETE /api/v1/org`). The DB schema already cascades
every tenant row from `organizations.id` (memberships, stores → cameras →
rag_cases, clips, alerts → feedback, agents, invitations, pairing codes), but a
cascade can't reach the .mp4 evidence files on disk — so this service collects
the org's clip `storage_path`s and unlinks them FIRST, then deletes the org row
in the same request transaction (committed by deps.db.get_db).

File-pass semantics mirror services.retention: a missing file is fine (already
gone), an unlink error is logged and counted but does NOT block the org delete —
the tenant's right to erasure outranks a stray locked file (which then needs
manual cleanup; the path is in the warning log).

NOTE on users/tokens: user rows are NOT org-scoped (a user may belong to several
orgs), so they survive; their JWTs stay decodable but every org-scoped request
403s once the membership rows cascade away.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.clip import Clip
from sentry_backend.db.models.organization import Organization
from sentry_backend.logging_setup import get_logger
from sentry_backend.services.retention import FileDeleteResult, delete_clip_file

log = get_logger("sentry_backend.org_delete")


@dataclass(frozen=True)
class ClipFileSweep:
    """Outcome of the on-disk pass: how many evidence files were removed,
    were already gone, or failed to unlink (logged for manual cleanup)."""

    removed: int = 0
    missing: int = 0
    failed: int = 0


def remove_clip_files(storage_paths: Iterable[str]) -> ClipFileSweep:
    """Unlink every clip file; never raises (per-file results are counted)."""
    removed = missing = failed = 0
    for path in storage_paths:
        result = delete_clip_file(path)
        if result is FileDeleteResult.deleted:
            removed += 1
        elif result is FileDeleteResult.missing:
            missing += 1
        else:
            failed += 1
    return ClipFileSweep(removed=removed, missing=missing, failed=failed)


async def delete_organization(db: AsyncSession, org: Organization) -> ClipFileSweep:
    """Delete `org`: evidence files first, then the row (DB cascades the rest).

    Only clips whose file is still expected on disk are unlinked
    (`file_deleted_at IS NULL` — retention may have removed the rest already).
    """
    rows = await db.execute(
        select(Clip.storage_path).where(
            Clip.organization_id == org.id,
            Clip.file_deleted_at.is_(None),
        )
    )
    sweep = remove_clip_files(rows.scalars().all())

    org_id, slug = str(org.id), org.slug
    await db.execute(delete(Organization).where(Organization.id == org.id))
    await db.flush()

    log.info(
        "org_deleted",
        org_id=org_id,
        slug=slug,
        clips_removed=sweep.removed,
        clip_files_missing=sweep.missing,
        clip_files_failed=sweep.failed,
    )
    return sweep
