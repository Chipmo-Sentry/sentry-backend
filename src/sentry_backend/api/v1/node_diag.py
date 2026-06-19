"""Node diagnostics — the rich per-stage "what is wrong right now" channel that
backs the /pipeline/stage diagnostics console (docs/26).

- The AI node PUSHES a snapshot (worker errors, VLM verdicts + raw-on-failure)
  to POST /api/v1/internal/node-diag (its own ai_node JWT).
- The customer app READS it org-scoped at GET /api/v1/nodes/{id}/diag — gated by
  the same derived org-ownership as the node list (404, not 403, for an unowned
  node so existence isn't leaked).
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.ai_node import AiNode
from sentry_backend.deps.ai_node_auth import get_current_ai_node
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import ai_node_repo, camera_repo
from sentry_backend.schemas.ai_node import parse_camera_health
from sentry_backend.services import node_diag_store

router = APIRouter(tags=["node-diag"])


@router.post("/api/v1/internal/node-diag", status_code=status.HTTP_204_NO_CONTENT)
async def push_node_diag(
    node: Annotated[AiNode, Depends(get_current_ai_node)],
    body: Annotated[dict[str, Any], Body()],
) -> None:
    """Store the latest diagnostics snapshot for the calling node (identified by
    its ai_node JWT). Body is the node's free-form diag JSON."""
    node_diag_store.put(node.id, body)


async def _org_owns_node(db: AsyncSession, org_id: UUID, node: AiNode) -> bool:
    """True if this node reports at least one of the caller-org's cameras (the same
    derived ownership the node list uses — AiNode has no org FK)."""
    cams = await camera_repo.list_cameras_for_org(db, org_id)
    allowed = {c.mediamtx_path for c in cams if c.mediamtx_path}
    if not allowed:
        return False
    node_cams = parse_camera_health(node.telemetry) or []
    return any(c.camera_id in allowed for c in node_cams)


@router.get("/api/v1/nodes/{node_id}/diag")
async def get_node_diag(
    node_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> dict[str, Any]:
    """Latest diagnostics for a node THIS org owns. `available=false` when the
    node has never pushed a diag (e.g. running an older build)."""
    node = await ai_node_repo.get_node(db, node_id)
    if node is None or not await _org_owns_node(db, org_id, node):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node олдсонгүй.")
    stored = node_diag_store.get(node_id)
    if stored is None:
        return {"available": False, "age_sec": None, "diag": None}
    import time

    received_at, diag = stored
    return {"available": True, "age_sec": int(time.time() - received_at), "diag": diag}
