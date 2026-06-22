"""Agent pairing + agent-facing camera registration.

Two audiences:
  • Admin (user cookie/JWT): generate pairing codes, list/revoke agents.
  • Agent (agent JWT):        pair, register cameras, heartbeat.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent
from sentry_backend.db.models.alert import AlertCategory, AlertLevel, AlertTrigger
from sentry_backend.db.models.event_log import EventSeverity, EventType
from sentry_backend.db.models.organization import OrganizationMember, OrgRole
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.agent_auth import get_current_agent
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.ratelimit import limiter
from sentry_backend.repository import (
    agent_repo,
    alert_repo,
    camera_repo,
    clip_repo,
    edge_config_repo,
)
from sentry_backend.schemas.agent import (
    AgentCameraCreate,
    AgentCameraUpdate,
    AgentPairRequest,
    AgentPairResult,
    AgentPublic,
    AgentStreamConfig,
    PairingCodePublic,
)
from sentry_backend.schemas.alert import AlertPublic
from sentry_backend.schemas.camera import CameraPublic
from sentry_backend.schemas.edge import EdgeConfigPayload, merged_edge_payload
from sentry_backend.security import create_agent_token
from sentry_backend.services import ai_service, alert_notify, event_log, live_provision
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level
from sentry_backend.services.clip_service import ClipTooLargeError, save_upload_to_disk
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
        zones=[z.model_dump() for z in body.zones] if body.zones is not None else None,
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
        zones=[z.model_dump() for z in body.zones] if body.zones is not None else None,
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
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> EdgeConfigPayload:
    """Edge Stage-1 tunables for the store agent's config-poller (ADR-0029 I3).

    Returns the store's overrides merged over the defaults, with a monotonic
    `version` the agent uses to re-apply only on a real change. A store with no
    override row serves the defaults at version 1. The agent's `from_dict` keeps
    its own defaults for any omitted field, so this stays forward-compatible.
    """
    row = await edge_config_repo.get_for_store(db, agent.store_id)
    if row is None:
        return EdgeConfigPayload()
    return merged_edge_payload(row.version, row.overrides)


@router.post("/agent/edge/clips", response_model=AlertPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(lambda: get_settings().edge_clip_rate_limit)
async def agent_edge_clip(
    request: Request,  # noqa: ARG001 — required by slowapi's limiter
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(get_current_agent)],
    clip: Annotated[UploadFile, File()],
    camera_uuid: Annotated[UUID, Form()],
    risk_pct: Annotated[float, Form()] = 0.0,
    behaviors: Annotated[str, Form()] = "",
    started_at: Annotated[float, Form()] = 0.0,
    ended_at: Annotated[float, Form()] = 0.0,
) -> AlertPublic:
    """Edge Stage-1 (ADR-0029): the store agent ran YOLO + behaviour LOCALLY and
    uploads ONE suspicious clip. We store it, get the VLM verdict from sentry-ai
    (bytes-forward — the GPU node can't read this host's disk), and create the
    alert. If the VLM is unavailable we still create a log-level behaviour alert
    so a flagged event is never lost (ADR §8.4); the clip is stored either way.
    """
    cam = await camera_repo.get_camera_for_org(db, camera_uuid, agent.organization_id)
    if cam is None or cam.store_id != agent.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Камер олдсонгүй.")

    try:
        storage_path, size_bytes, sha256 = await save_upload_to_disk(
            clip, organization_id=agent.organization_id
        )
    except ClipTooLargeError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Клипийн хэмжээ хэтэрсэн байна.",
        ) from e

    captured = datetime.fromtimestamp(started_at, tz=UTC) if started_at else datetime.now(UTC)
    clip_row = await clip_repo.create_clip(
        db,
        organization_id=agent.organization_id,
        store_id=agent.store_id,
        camera_id=cam.id,
        captured_at=captured,
        duration_sec=max(0.0, ended_at - started_at),
        storage_path=str(storage_path),
        file_size_bytes=size_bytes,
        sha256=sha256,
    )
    await agent_repo.touch_last_seen(db, agent)
    await db.commit()

    behaviors_list = [b for b in behaviors.split(",") if b]
    verdict = await ai_service.verify_edge_clip(storage_path, store_id=str(agent.store_id))

    # Defaults = the VLM-unavailable fallback (behaviour-only, log level).
    parsed = False
    category = AlertCategory.other
    confidence = max(0.0, min(1.0, risk_pct / 100.0))
    reasoning = "VLM боломжгүй — edge зан төлөвөөр (clip хадгалсан)."
    model_name = "edge-behaviour"
    latency_ms = 0
    embedding: list[float] | None = None
    if verdict is not None:
        try:
            category = AlertCategory(str(verdict["category"]))
            confidence = float(verdict["confidence"])  # type: ignore[arg-type]
            reasoning = str(verdict.get("reasoning", ""))
            model_name = str(verdict.get("model_name", "edge"))
            latency_ms = int(verdict["inference_latency_ms"])  # type: ignore[call-overload]
            emb = verdict.get("embedding")
            embedding = [float(x) for x in emb] if isinstance(emb, list) else None
            parsed = True
        except (KeyError, ValueError, TypeError):
            category = AlertCategory.other
            confidence = max(0.0, min(1.0, risk_pct / 100.0))
            reasoning = "VLM хариу буруу — edge зан төлөвөөр."
            embedding = None

    alert_level = derive_alert_level(category, confidence) if parsed else AlertLevel.log
    alert = await alert_repo.create_alert(
        db,
        clip_id=clip_row.id,
        organization_id=agent.organization_id,
        store_id=agent.store_id,
        camera_id=cam.id,
        category=category,
        confidence=confidence,
        reasoning=reasoning,
        model_name=model_name,
        alert_level=alert_level,
        inference_latency_ms=latency_ms,
        triggered_by=AlertTrigger.edge_pc_upload,
        peak_risk_pct=risk_pct,
        triggered_behaviors=behaviors_list or None,
        embedding=embedding,
    )
    alert_public = AlertPublic.model_validate(alert)
    # I2 — best-effort Telegram for actionable edge alerts (gated to notify/review
    # inside notify_alert; the store lookup uses this session).
    await alert_notify.notify_alert(db, alert)
    await db.commit()
    # I1 — push to the org's SSE subscribers so edge alerts appear live on the
    # dashboard exactly like cloud-path (verify_clip_with_ai) alerts.
    await get_broker().publish(alert_public.organization_id, alert_public.model_dump(mode="json"))
    return alert_public


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
