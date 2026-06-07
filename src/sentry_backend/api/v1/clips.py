"""Clips router — multipart upload + listing + download."""

import asyncio
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import (
    get_current_organization_id,
    get_current_organization_id_admin,
)
from sentry_backend.repository import camera_repo, clip_repo
from sentry_backend.schemas.clip import ClipPublic
from sentry_backend.services.ai_service import verify_clip_with_ai
from sentry_backend.services.clip_service import (
    ClipTooLargeError,
    save_upload_to_disk,
)

router = APIRouter(prefix="/api/v1/clips", tags=["clips"])


@router.post("", response_model=ClipPublic, status_code=status.HTTP_201_CREATED)
async def upload_clip(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
    file: Annotated[UploadFile, File()],
    store_id: Annotated[UUID, Form()],
    camera_id: Annotated[UUID | None, Form()] = None,
    captured_at: Annotated[datetime | None, Form()] = None,
    duration_sec: Annotated[float, Form()] = 0.0,
) -> ClipPublic:
    """Multipart upload of an mp4. Triggers sentry-ai via separate /verify call."""
    # Validate store belongs to org
    if not await camera_repo.store_belongs_to_org(db, store_id, org_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Store not found or not in your organization",
        )

    # Validate camera (if specified) belongs to the store
    if camera_id is not None:
        cam = await camera_repo.get_camera_for_org(db, camera_id, org_id)
        if cam is None or cam.store_id != store_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Camera not found or not in specified store",
            )

    try:
        storage_path, size_bytes, sha256 = await save_upload_to_disk(file, organization_id=org_id)
    except ClipTooLargeError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Clip exceeds max size",
        ) from e

    # Dedup by sha256 within org
    existing = await clip_repo.find_clip_by_sha256_for_org(db, sha256, org_id)
    if existing is not None:
        storage_path.unlink(missing_ok=True)
        return ClipPublic.model_validate(existing)

    clip = await clip_repo.create_clip(
        db,
        organization_id=org_id,
        store_id=store_id,
        camera_id=camera_id,
        captured_at=captured_at or datetime.now(UTC),
        duration_sec=duration_sec,
        storage_path=str(storage_path),
        file_size_bytes=size_bytes,
        sha256=sha256,
    )

    # Fire-and-forget: the user's upload is done; sentry-ai will verify in
    # the background and emit an Alert via SSE when it finishes (~5-15s).
    # The task is detached from the request so we return ClipPublic now.
    asyncio.create_task(verify_clip_with_ai(clip.id))  # noqa: RUF006

    return ClipPublic.model_validate(clip)


@router.get("", response_model=list[ClipPublic])
async def list_clips(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    store_id: Annotated[UUID | None, Query()] = None,
    camera_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ClipPublic]:
    clips = await clip_repo.list_clips_for_org(
        db, org_id, store_id=store_id, camera_id=camera_id, limit=limit, offset=offset
    )
    return [ClipPublic.model_validate(c) for c in clips]


@router.get("/{clip_id}", response_model=ClipPublic)
async def get_clip(
    clip_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> ClipPublic:
    clip = await clip_repo.get_clip_for_org(db, clip_id, org_id)
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    return ClipPublic.model_validate(clip)


@router.get("/{clip_id}/download")
async def download_clip(
    clip_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> FileResponse:
    clip = await clip_repo.get_clip_for_org(db, clip_id, org_id)
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    return FileResponse(
        path=clip.storage_path,
        media_type="video/mp4",
        filename=f"{clip.id}.mp4",
    )
