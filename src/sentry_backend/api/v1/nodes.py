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
from sentry_backend.repository import ai_node_repo, camera_repo
from sentry_backend.schemas.ai_node import OrgNodePublic, build_org_node, parse_camera_health

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])

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
