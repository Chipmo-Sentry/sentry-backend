"""Cameras router — org-scoped CRUD (via Store join)."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.api.v1.live_proxy import node_whep_base
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import (
    get_current_organization_id,
    get_current_organization_id_admin,
)
from sentry_backend.repository import camera_repo
from sentry_backend.schemas.camera import (
    CameraCreate,
    CameraPublic,
    CameraUpdate,
    StreamTokenResponse,
)
from sentry_backend.security import create_livekit_token, create_stream_token
from sentry_backend.services import live_provision
from sentry_backend.services.billing.gating import SUSPENDED_DETAIL, org_billing_status
from sentry_backend.services.billing.status import BillingStatus
from sentry_backend.settings import get_settings

router = APIRouter(prefix="/api/v1/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraPublic])
async def list_cameras(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    store_id: Annotated[UUID | None, Query()] = None,
) -> list[CameraPublic]:
    cams = await camera_repo.list_cameras_for_org(db, org_id, store_id=store_id)
    return [CameraPublic.from_orm_camera(c) for c in cams]


@router.post("", response_model=CameraPublic, status_code=status.HTTP_201_CREATED)
async def create_camera(
    body: CameraCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
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
        mediamtx_path=body.mediamtx_path,
        risk_threshold=body.risk_threshold,
        compute_tier=body.compute_tier.value,
        zones=[z.model_dump() for z in body.zones] if body.zones is not None else None,
    )
    if cam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Дэлгүүр олдсонгүй эсвэл таны байгууллагынх биш байна.",
        )
    # Commit before MediaMTX sync so the path exists in DB even if MediaMTX
    # is unreachable (operator can restart MediaMTX to pick up later).
    await db.commit()
    # ADR-0029: edge cameras run Stage-1 locally — skip the cloud live worker +
    # MediaMTX pull. Only cloud-tier cameras provision the cloud pipeline.
    if cam.topology_mode == "cloud" and cam.mediamtx_path and body.rtsp_url:
        await live_provision.provision(
            cam.mediamtx_path,
            body.rtsp_url,
            enabled=cam.enabled,
            store_id=str(cam.store_id),
            risk_threshold=cam.risk_threshold,
            zones=cam.zones,
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Камер олдсонгүй.")
    return CameraPublic.from_orm_camera(cam)


@router.get("/{camera_id}/stream-token", response_model=StreamTokenResponse)
async def get_stream_token(
    camera_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> StreamTokenResponse:
    """Mint a short-lived WHEP/HLS read token for a camera the caller owns.

    Any org member (read access) may watch; the token confines playback to this
    camera's mediamtx_path and is validated by the MediaMTX authHTTP endpoint.
    """
    # T14 gating: live viewing stops for a suspended org. Evidence recording
    # (alerts/clips) is deliberately NOT gated — нотолгоо үргэлжилнэ.
    if await org_billing_status(db, org_id) is BillingStatus.suspended:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=SUSPENDED_DETAIL,
        )
    cam = await camera_repo.get_camera_for_org(db, camera_id, org_id)
    if cam is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Камер олдсонгүй.")
    if not cam.mediamtx_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ камерт шууд дамжуулалтын зам тохируулаагүй байна.",
        )
    token = create_stream_token(cam.mediamtx_path)
    whep_base = await node_whep_base(db, cam.mediamtx_path)
    # Viewer identity must be unique per connection: LiveKit disconnects an
    # older participant when a new one joins with the same identity.
    livekit_token = create_livekit_token(
        cam.mediamtx_path, f"viewer-{uuid4().hex[:12]}"
    )
    return StreamTokenResponse(
        token=token,
        expires_in=get_settings().stream_token_ttl_sec,
        hls_url=f"/api/v1/live/{cam.mediamtx_path}/hls/index.m3u8?jwt={token}",
        whep_url=(
            f"{whep_base}/{cam.mediamtx_path}{get_settings().whep_low_suffix}/whep?jwt={token}"
            if whep_base
            else None
        ),
        livekit_url=get_settings().livekit_url or None,
        livekit_token=livekit_token,
    )


@router.patch("/{camera_id}", response_model=CameraPublic)
async def update_camera(
    camera_id: UUID,
    body: CameraUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> CameraPublic:
    cam = await camera_repo.get_camera_for_org(db, camera_id, org_id)
    if cam is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Камер олдсонгүй.")
    old_path = cam.mediamtx_path
    cam = await camera_repo.update_camera(
        db,
        cam,
        name=body.name,
        rtsp_url=body.rtsp_url,
        shelf_zone_json=body.shelf_zone_json,
        stage2_threshold=body.stage2_threshold,
        enabled=body.enabled,
        mediamtx_path=body.mediamtx_path,
        risk_threshold=body.risk_threshold,
        compute_tier=body.compute_tier.value if body.compute_tier else None,
        zones=[z.model_dump() for z in body.zones] if body.zones is not None else None,
    )
    await db.commit()

    # Live pipeline sync: tear down the old path if renamed, then (re)provision
    # MediaMTX + AI worker for the current path/enabled state.
    if old_path and old_path != cam.mediamtx_path:
        await live_provision.deprovision(old_path)
    if cam.topology_mode != "cloud":
        # ADR-0029: edge cameras run Stage-1 locally — never a cloud worker.
        # Tear one down in case the camera was just switched cloud → edge.
        if cam.mediamtx_path:
            await live_provision.deprovision(cam.mediamtx_path)
    elif cam.mediamtx_path:
        rtsp = await camera_repo.decrypt_rtsp_url(cam)
        if rtsp:
            await live_provision.provision(
                cam.mediamtx_path,
                rtsp,
                enabled=cam.enabled,
                store_id=str(cam.store_id),
                risk_threshold=cam.risk_threshold,
                zones=cam.zones,
            )
    return CameraPublic.from_orm_camera(cam)


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> None:
    cam = await camera_repo.get_camera_for_org(db, camera_id, org_id)
    if cam is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Камер олдсонгүй.")
    path = cam.mediamtx_path
    await camera_repo.delete_camera(db, cam)
    await db.commit()
    if path:
        await live_provision.deprovision(path)
