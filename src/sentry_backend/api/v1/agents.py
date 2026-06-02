"""Agent pairing + agent-facing camera registration.

Two audiences:
  • Admin (user cookie/JWT): generate pairing codes, list/revoke agents.
  • Agent (agent JWT):        pair, register cameras, heartbeat.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent
from sentry_backend.db.models.organization import OrganizationMember, OrgRole
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.agent_auth import get_current_agent
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.repository import agent_repo, camera_repo
from sentry_backend.schemas.agent import (
    AgentCameraCreate,
    AgentPairRequest,
    AgentPairResult,
    AgentPublic,
    PairingCodePublic,
)
from sentry_backend.schemas.camera import CameraPublic
from sentry_backend.security import create_agent_token
from sentry_backend.services import mediamtx_client

router = APIRouter(prefix="/api/v1", tags=["agents"])

_ADMIN_ROLES = {OrgRole.owner, OrgRole.admin}


async def _require_store_admin(
    db: AsyncSession, user: User, store_id: UUID
) -> Store:
    """Return the store iff `user` may administer it (super-admin, or owner/
    admin of the store's org). Raises 404 / 403 otherwise."""
    store = (
        await db.execute(select(Store).where(Store.id == store_id))
    ).scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
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
            detail="Admin or owner role required for this store",
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
    return PairingCodePublic(
        code=code.code, store_id=store.id, expires_at=code.expires_at
    )


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    await _require_store_admin(db, user, agent.store_id)
    await agent_repo.deactivate_agent(db, agent)


# ── Agent: pairing ──────────────────────────────────────────────────────
@router.post("/agents/pair", response_model=AgentPairResult)
async def pair_agent(
    body: AgentPairRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentPairResult:
    pairing = await agent_repo.consume_pairing_code(db, body.code.strip())
    if pairing is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired pairing code",
        )
    store = (
        await db.execute(select(Store).where(Store.id == pairing.store_id))
    ).scalar_one_or_none()
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Store no longer exists"
        )
    agent = await agent_repo.create_agent(
        db,
        organization_id=pairing.organization_id,
        store_id=pairing.store_id,
        name=body.name,
        paired_by_user_id=pairing.created_by_user_id,
    )
    await agent_repo.mark_consumed(db, pairing, agent.id)
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


@router.post(
    "/agent/cameras", response_model=CameraPublic, status_code=status.HTTP_201_CREATED
)
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
    )
    if cam is None:
        # Store vanished or got reassigned out of the agent's org.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent's store is no longer valid",
        )
    await agent_repo.touch_last_seen(db, agent)
    await db.commit()
    if cam.mediamtx_path and body.rtsp_url and cam.enabled:
        await mediamtx_client.add_path(cam.mediamtx_path, body.rtsp_url)
    return CameraPublic.from_orm_camera(cam)


@router.delete("/agent/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def agent_delete_camera(
    camera_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> None:
    cam = await camera_repo.get_camera_for_org(db, camera_id, agent.organization_id)
    if cam is None or cam.store_id != agent.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    path = cam.mediamtx_path
    await camera_repo.delete_camera(db, cam)
    await agent_repo.touch_last_seen(db, agent)
    await db.commit()
    if path:
        await mediamtx_client.delete_path(path)


@router.post("/agent/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def agent_heartbeat(
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> None:
    await agent_repo.touch_last_seen(db, agent)
