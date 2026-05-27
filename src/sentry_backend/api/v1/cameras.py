"""Cameras router — org-scoped CRUD (via Store join)."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import camera_repo
from sentry_backend.schemas.camera import CameraCreate, CameraPublic, CameraUpdate

router = APIRouter(prefix="/api/v1/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraPublic])
async def list_cameras(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    store_id: Annotated[UUID | None, Query()] = None,
) -> list[CameraPublic]:
    cams = await camera_repo.list_cameras_for_org(db, org_id, store_id=store_id)
    return [CameraPublic.from_orm_camera(c) for c in cams]


@router.post(
    "", response_model=CameraPublic, status_code=status.HTTP_201_CREATED
)
async def create_camera(
    body: CameraCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> CameraPublic:
    cam = await camera_repo.create_camera(
        db,
        store_id=body.store_id,
        org_id=org_id,
        name=body.name,
        rtsp_url=body.rtsp_url,
        shelf_zone_json=body.shelf_zone_json,
        stage2_threshold=body.stage2_threshold,
        enabled=body.enabled,
    )
    if cam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Store not found or not in your organization",
        )
    return CameraPublic.from_orm_camera(cam)


@router.get("/{camera_id}", response_model=CameraPublic)
async def get_camera(
    camera_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> CameraPublic:
    cam = await camera_repo.get_camera_for_org(db, camera_id, org_id)
    if cam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found"
        )
    return CameraPublic.from_orm_camera(cam)


@router.patch("/{camera_id}", response_model=CameraPublic)
async def update_camera(
    camera_id: UUID,
    body: CameraUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> CameraPublic:
    cam = await camera_repo.get_camera_for_org(db, camera_id, org_id)
    if cam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found"
        )
    cam = await camera_repo.update_camera(
        db,
        cam,
        name=body.name,
        rtsp_url=body.rtsp_url,
        shelf_zone_json=body.shelf_zone_json,
        stage2_threshold=body.stage2_threshold,
        enabled=body.enabled,
    )
    return CameraPublic.from_orm_camera(cam)


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> None:
    cam = await camera_repo.get_camera_for_org(db, camera_id, org_id)
    if cam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found"
        )
    await camera_repo.delete_camera(db, cam)
