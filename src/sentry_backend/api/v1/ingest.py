"""Org-scoped cloud-ingest (MediaMTX) path state for the Pipeline Canvas.

Closes the synthesis gap at hop #4: mediamtx-auth only gives instantaneous
allow/deny, so the backend had no view of which paths actually have a publisher.
This proxies MediaMTX's runtime GET /v3/paths/list and projects it to the
caller-org's cameras (by mediamtx_path), so stage 2 ("video arriving at cloud")
can light up instead of being permanently "unknown".
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.repository import camera_repo
from sentry_backend.schemas.ingest import IngestPath, IngestPathsResponse
from sentry_backend.services import mediamtx_client

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


@router.get("/paths", response_model=IngestPathsResponse)
async def list_ingest_paths(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> IngestPathsResponse:
    """Runtime publish state of THIS org's camera paths at the cloud ingest.

    Any org member (read access) may view. `available=False` ⇒ MediaMTX API
    disabled/unreachable (stage 2 stays "unknown", never a fake "0 ready")."""
    cams = await camera_repo.list_cameras_for_org(db, org_id)
    org_paths = {c.mediamtx_path: c.name for c in cams if c.mediamtx_path}
    if not org_paths:
        return IngestPathsResponse(available=True, paths=[])

    state = await mediamtx_client.list_paths()
    if state is None:
        return IngestPathsResponse(available=False, paths=[])

    paths = [
        IngestPath(
            path=p,
            name=org_paths[p],
            ready=bool(s.get("ready")),
            readers=int(s.get("readers", 0)),
        )
        for p, s in state.items()
        if p in org_paths
    ]
    paths.sort(key=lambda x: x.name)
    return IngestPathsResponse(available=True, paths=paths)
