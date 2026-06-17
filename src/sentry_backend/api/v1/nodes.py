"""Org-scoped AI-node health read for the customer app (Pipeline Canvas / Health).

AiNode rows are super-admin infrastructure with NO organization_id — a node
serves cameras across orgs and is linked to an org ONLY through
telemetry.cameras[].camera_id == Camera.mediamtx_path. So this endpoint DERIVES
ownership (the caller-org's cameras) and PROJECTS each node down to that org:
it returns only the org's cameras out of a shared node and never leaks the raw
telemetry string. See schemas.ai_node.build_org_node.

Superadmin fleet management stays at /api/v1/admin/ai-nodes (admin.py).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import ai_node_repo, camera_repo
from sentry_backend.schemas.ai_node import OrgNodePublic, build_org_node

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


@router.get("", response_model=list[OrgNodePublic])
async def list_org_nodes(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> list[OrgNodePublic]:
    """AI nodes that serve at least one of this org's cameras, projected to the
    org's cameras only. Any org member (read access) may view node health."""
    cams = await camera_repo.list_cameras_for_org(db, org_id)
    allowed_paths = {c.mediamtx_path for c in cams if c.mediamtx_path}
    if not allowed_paths:
        return []
    nodes = await ai_node_repo.list_nodes(db)
    out: list[OrgNodePublic] = []
    for node in nodes:
        projected = build_org_node(node, allowed_paths)
        if projected is not None:
            out.append(projected)
    return out
