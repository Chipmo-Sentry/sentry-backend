"""Org-scoped AI-node health read for the customer app (Pipeline Canvas / Health).

AiNode rows are super-admin infrastructure with NO organization_id — a node
serves cameras across orgs and is linked to an org ONLY through
telemetry.cameras[].camera_id == Camera.mediamtx_path. So this endpoint DERIVES
ownership (the caller-org's cameras) and PROJECTS each node down to that org:
it returns only the org's cameras out of a shared node and never leaks the raw
telemetry string. See schemas.ai_node.build_org_node.

Superadmin fleet management stays at /api/v1/admin/ai-nodes (admin.py).
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import agent_repo, ai_node_repo, camera_repo
from sentry_backend.schemas.agent import AgentPushPath, AgentPushStatusResponse
from sentry_backend.schemas.ai_node import OrgNodePublic, build_org_node, parse_camera_health

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])

# An agent heartbeats every ~30s; treat it offline if we haven't heard in 90s.
_AGENT_ONLINE_WINDOW = timedelta(seconds=90)

_METRIC_SPANS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


async def _org_paths(db: AsyncSession, org_id: UUID) -> set[str]:
    cams = await camera_repo.list_cameras_for_org(db, org_id)
    return {c.mediamtx_path for c in cams if c.mediamtx_path}


@router.get("", response_model=list[OrgNodePublic])
async def list_org_nodes(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> list[OrgNodePublic]:
    """AI nodes that serve at least one of this org's cameras, projected to the
    org's cameras only. Any org member (read access) may view node health."""
    allowed_paths = await _org_paths(db, org_id)
    if not allowed_paths:
        return []
    nodes = await ai_node_repo.list_nodes(db)
    out: list[OrgNodePublic] = []
    for node in nodes:
        projected = build_org_node(node, allowed_paths)
        if projected is not None:
            out.append(projected)
    return out


@router.get("/agent-push", response_model=AgentPushStatusResponse)
async def org_agent_push(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> AgentPushStatusResponse:
    """Per-camera ffmpeg push-relay state across this org's store agents, keyed by
    mediamtx_path. Drives the 'Камер' pipeline stage: a down relay shows the real
    reason (last_error) instead of just 'кадр зогссон'. Cameras whose agent never
    reported push (pull/on-LAN topology, older agent) simply don't appear here, so
    the frontend keeps its WS-based fallback for them."""
    now = datetime.now(UTC)
    agents = await agent_repo.list_active_agents_for_org(db, org_id)
    out: list[AgentPushPath] = []
    for agent in agents:
        if not agent.push_status:
            continue
        online = agent.last_seen_at is not None and now - agent.last_seen_at < _AGENT_ONLINE_WINDOW
        for entry in agent.push_status:
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                continue
            out.append(
                AgentPushPath(
                    path=path,
                    running=bool(entry.get("running")),
                    restarts=int(entry.get("restarts") or 0),
                    last_error=entry.get("last_error"),
                    agent_id=agent.id,
                    agent_name=agent.name,
                    agent_online=online,
                )
            )
    return AgentPushStatusResponse(paths=out)


@router.get("/{node_id}/metrics")
async def org_node_metrics(
    node_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    range_: Annotated[str, Query(alias="range")] = "24h",
    bucket: str = "auto",
) -> list[dict[str, object]]:
    """Resource time-series (CPU/RAM/GPU/VRAM/FPS) for a node THIS org owns.

    Org ownership is derived (same as the node list): the node must report at
    least one of the caller-org's cameras. A node the org doesn't own returns
    404 (not 403) so node existence isn't leaked across tenants.
    `range` = 1h | 6h | 24h | 7d | 30d.
    """
    allowed_paths = await _org_paths(db, org_id)
    node = await ai_node_repo.get_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node олдсонгүй.")
    node_cams = parse_camera_health(node.telemetry) or []
    if not allowed_paths or not any(c.camera_id in allowed_paths for c in node_cams):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node олдсонгүй.")

    span = _METRIC_SPANS.get(range_, _METRIC_SPANS["24h"])
    to = datetime.now(UTC)
    frm = to - span
    if bucket == "auto":
        bucket = "hour" if span > timedelta(hours=24) else "raw"
    return await ai_node_repo.get_metrics(db, node_id, frm=frm, to=to, bucket=bucket)
