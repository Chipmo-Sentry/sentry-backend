"""Agent pairing + agent-facing camera registration.

Two audiences:
  • Admin (user cookie/JWT): generate pairing codes, list/revoke agents.
  • Agent (agent JWT):        pair, register cameras, heartbeat.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent
from sentry_backend.db.models.event_log import EventSeverity, EventType
from sentry_backend.db.models.organization import OrganizationMember, OrgRole
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.agent_auth import get_current_agent
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.ratelimit import limiter
from sentry_backend.repository import agent_repo, camera_repo
from sentry_backend.schemas.agent import (
    AgentCameraCreate,
    AgentCameraUpdate,
    AgentPairRequest,
    AgentPairResult,
    AgentPublic,
    AgentStreamConfig,
    PairingCodePublic,
)
from sentry_backend.schemas.camera import CameraPublic
from sentry_backend.schemas.edge import EdgeConfigPayload
from sentry_backend.security import create_agent_token
from sentry_backend.services import event_log, live_provision
from sentry_backend.settings import get_settings

router = APIRouter(prefix="/api/v1", tags=["agents"])

_ADMIN_ROLES = {OrgRole.owner, OrgRole.admin}


async def _require_store_admin(db: AsyncSession, user: User, store_id: UUID) -> Store:
    """Return the store iff `user` may administer it (super-admin, or owner/
    admin of the store's org). Raises 404 / 403 otherwise."""
    store = (await db.execute(select(Store).where(Store.id == store_id))).scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр олдсонгүй.")
    if user.is_super_admin:
        return store
    role = (
        await db.execute(
            select(OrganizationMember.role).where(
                OrganizationMember.user_id == user.id,
                OrganizationMember.organization_id == store.organization_id,
            )
        )
    ).scalar_one_or_none()
    if role not in _ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Энэ дэлгүүрт admin эсвэл эзэн (owner) эрх шаардлагатай.",
        )
    return store


# ── Admin: pairing codes + agent management ─────────────────────────────
@router.post(
    "/stores/{store_id}/pairing-codes",
    response_model=PairingCodePublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_pairing_code(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> PairingCodePublic:
    store = await _require_store_admin(db, user, store_id)
    code = await agent_repo.create_pairing_code(
        db,
        organization_id=store.organization_id,
        store_id=store.id,
        created_by_user_id=user.id,
    )
    return PairingCodePublic(code=code.code, store_id=store.id, expires_at=code.expires_at)


@router.get("/stores/{store_id}/agents", response_model=list[AgentPublic])
async def list_store_agents(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> list[AgentPublic]:
    await _require_store_admin(db, user, store_id)
    agents = await agent_repo.list_agents_for_store(db, store_id)
    return [AgentPublic.model_validate(a) for a in agents]


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_agent(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    agent = await agent_repo.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Холбогдсон компьютер олдсонгүй."
        )
    await _require_store_admin(db, user, agent.store_id)
    await agent_repo.delete_agent(db, agent)


# ── Agent: pairing ──────────────────────────────────────────────────────
@router.post("/agents/pair", response_model=AgentPairResult)
@limiter.limit(lambda: get_settings().pair_rate_limit)
async def pair_agent(
    request: Request,  # noqa: ARG001 — required by slowapi's limiter
    body: AgentPairRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentPairResult:
    pairing = await agent_repo.consume_pairing_code(db, body.code.strip())
    if pairing is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Холболтын код буруу эсвэл хугацаа нь дууссан байна.",
        )
    store = (
        await db.execute(select(Store).where(Store.id == pairing.store_id))
    ).scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дэлгүүр устсан байна.")
    # Re-pairing the SAME computer (matched by hostname within the store) reuses
    # its existing row instead of spawning a duplicate "connected computer" —
    # the agent already sends name=hostname. New token is issued below either way.
    agent = None
    if body.name:
        agent = await agent_repo.get_agent_by_name(db, pairing.store_id, body.name)
    if agent is not None:
        await agent_repo.reactivate_agent(db, agent)
    else:
        agent = await agent_repo.create_agent(
            db,
            organization_id=pairing.organization_id,
            store_id=pairing.store_id,
            name=body.name,
            paired_by_user_id=pairing.created_by_user_id,
        )
    await agent_repo.mark_consumed(db, pairing, agent.id)
    await event_log.emit(
        db,
        event_type=EventType.agent_paired,
        severity=EventSeverity.success,
        message=f"Десктоп апп холбогдлоо: {agent.name or 'нэргүй'} ({store.name})",
        organization_id=agent.organization_id,
        store_id=agent.store_id,
        agent_id=agent.id,
        actor_label=agent.name or "agent",
    )
    return AgentPairResult(
        agent_token=create_agent_token(agent.id),
        agent_id=agent.id,
        organization_id=agent.organization_id,
        store_id=agent.store_id,
        store_name=store.name,
    )


# ── Agent: cameras + heartbeat ──────────────────────────────────────────
@router.get("/agent/cameras", response_model=list[CameraPublic])
async def agent_list_cameras(
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> list[CameraPublic]:
    cams = await camera_repo.list_cameras_for_org(
        db, agent.organization_id, store_id=agent.store_id
    )
    return [CameraPublic.from_orm_camera(c) for c in cams]


@router.post("/agent/cameras", response_model=CameraPublic, status_code=status.HTTP_201_CREATED)
async def agent_register_camera(
    body: AgentCameraCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> CameraPublic:
    cam = await camera_repo.create_camera(
        db,
        store_id=agent.store_id,
        org_id=agent.organization_id,
        name=body.name,
        rtsp_url=body.rtsp_url,
        shelf_zone_json=None,
        stage2_threshold=0.6,
        enabled=True,
        mediamtx_path=body.mediamtx_path,
        risk_threshold=body.risk_threshold,
        compute_tier=body.compute_tier.value,
    )
    if cam is None:
        # Store vanished or got reassigned out of the agent's org.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ компьютерт холбогдсон дэлгүүр хүчингүй болсон байна.",
        )
    await agent_repo.touch_last_seen(db, agent)
    await event_log.emit(
        db,
        event_type=EventType.camera_registered,
        severity=EventSeverity.success,
        message=f"Камер холбогдлоо: {cam.name}",
        organization_id=agent.organization_id,
        store_id=agent.store_id,
        camera_id=cam.id,
        agent_id=agent.id,
        actor_label=agent.name or "agent",
        detail={"mediamtx_path": cam.mediamtx_path},
    )
    await db.commit()
    # ADR-0029: only cloud-tier cameras provision the cloud live worker; edge
    # cameras run Stage-1 on the store device.
    if cam.topology_mode == "cloud" and cam.mediamtx_path and body.rtsp_url:
        await live_provision.provision(
            cam.mediamtx_path,
            body.rtsp_url,
            enabled=cam.enabled,
            store_id=str(cam.store_id),
            risk_threshold=cam.risk_threshold,
        )
    return CameraPublic.from_orm_camera(cam)


@router.patch("/agent/cameras/{camera_id}", response_model=CameraPublic)
async def agent_update_camera(
    camera_id: UUID,
    body: AgentCameraUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> CameraPublic:
    """Edit a camera's connection (rtsp_url), display name, or risk threshold.

    Used by the desktop agent's 'Засах' (edit) flow when a camera's IP or
    credentials change — so the user can fix the connection without delete +
    re-add (which would churn the UUID + mediamtx_path). When the rtsp_url
    changes we re-provision the live worker so the AI pulls the new source.
    """
    cam = await camera_repo.get_camera_for_org(db, camera_id, agent.organization_id)
    if cam is None or cam.store_id != agent.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Камер олдсонгүй.")
    rtsp_changed = body.rtsp_url is not None and body.rtsp_url != ""
    cam = await camera_repo.update_camera(
        db,
        cam,
        name=body.name,
        rtsp_url=body.rtsp_url,
        risk_threshold=body.risk_threshold,
        compute_tier=body.compute_tier.value if body.compute_tier else None,
    )
    await agent_repo.touch_last_seen(db, agent)
    await db.commit()
    # Re-provision when the source OR the risk threshold changed, so the AI
    # follows the edit (the node reads the threshold at worker start). On a
    # threshold-only edit body.rtsp_url is None → use the stored encrypted URL.
    threshold_changed = body.risk_threshold is not None
    # ADR-0029: never (re)provision a cloud worker for an edge-tier camera.
    if cam.topology_mode == "cloud" and cam.mediamtx_path and (rtsp_changed or threshold_changed):
        rtsp = body.rtsp_url if rtsp_changed else await camera_repo.decrypt_rtsp_url(cam)
        if rtsp:
            await live_provision.provision(
                cam.mediamtx_path,
                rtsp,
                enabled=cam.enabled,
                store_id=str(cam.store_id),
                risk_threshold=cam.risk_threshold,
            )
    return CameraPublic.from_orm_camera(cam)


@router.delete("/agent/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def agent_delete_camera(
    camera_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> None:
    cam = await camera_repo.get_camera_for_org(db, camera_id, agent.organization_id)
    if cam is None or cam.store_id != agent.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Камер олдсонгүй.")
    path = cam.mediamtx_path
    await camera_repo.delete_camera(db, cam)
    await agent_repo.touch_last_seen(db, agent)
    await db.commit()
    if path:
        await live_provision.deprovision(path)


@router.get("/agent/stream-config", response_model=AgentStreamConfig)
async def agent_stream_config(
    _agent: Annotated[Agent, Depends(get_current_agent)],
) -> AgentStreamConfig:
    """Tell a paired agent where (and whether) to publish its camera streams.

    push_enabled=True → cloud topology: the agent runs ffmpeg relays pushing
    each LAN camera to `push_rtsp_base/<mediamtx_path>`. False → MediaMTX pulls
    cameras directly (local/on-LAN) and the agent pushes nothing.
    """
    s = get_settings()
    return AgentStreamConfig(
        push_enabled=bool(s.agent_stream_push_url),
        push_rtsp_base=s.agent_stream_push_url,
        publish_user=s.mediamtx_publish_user,
        publish_pass=s.mediamtx_publish_pass,
    )


@router.get("/agent/edge-config", response_model=EdgeConfigPayload)
async def agent_edge_config(
    _agent: Annotated[Agent, Depends(get_current_agent)],
) -> EdgeConfigPayload:
    """Edge Stage-1 tunables for the store agent's config-poller (ADR-0029).

    v1 serves the defaults; a per-store EdgeConfig override + version bump lands
    with the superadmin EdgeConfig CRUD. The agent's `from_dict` keeps its own
    defaults for any field omitted here, so this stays forward-compatible.
    """
    return EdgeConfigPayload()


@router.post("/agent/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def agent_heartbeat(
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> None:
    await agent_repo.touch_last_seen(db, agent)
    await event_log.emit(
        db,
        event_type=EventType.agent_heartbeat,
        message=f"Десктоп апп асаалттай: {agent.name or 'нэргүй'}",
        organization_id=agent.organization_id,
        store_id=agent.store_id,
        agent_id=agent.id,
        actor_label=agent.name or "agent",
        is_heartbeat=True,
    )
