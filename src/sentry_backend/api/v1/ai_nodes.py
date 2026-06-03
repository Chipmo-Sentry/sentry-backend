"""AI node pairing + node-facing heartbeat/config.

Two audiences:
  - AI node (ai_node JWT): pair, heartbeat, poll config.
  - Super-admin manages nodes via /api/v1/admin/ai-nodes (see admin.py).
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.ai_node import AiNode
from sentry_backend.deps.ai_node_auth import get_current_ai_node
from sentry_backend.deps.db import get_db
from sentry_backend.repository import ai_node_repo
from sentry_backend.schemas.ai_node import (
    AiNodeConfig,
    AiNodeHeartbeat,
    AiNodePairRequest,
    AiNodePairResult,
)
from sentry_backend.security import create_ai_node_token

router = APIRouter(prefix="/api/v1/ai-nodes", tags=["ai-nodes"])


def _config(node: AiNode) -> AiNodeConfig:
    return AiNodeConfig(enabled=node.enabled, provider=node.provider, frame_skip=node.frame_skip)


@router.post("/pair", response_model=AiNodePairResult)
async def pair_ai_node(
    body: AiNodePairRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AiNodePairResult:
    pairing = await ai_node_repo.consume_pairing_code(db, body.code.strip())
    if pairing is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired pairing code",
        )
    node = await ai_node_repo.create_node(
        db,
        name=body.name,
        hostname=body.hostname,
        public_url=body.public_url,
        version=body.version,
        gpu=body.gpu,
        paired_by_user_id=pairing.created_by_user_id,
    )
    await ai_node_repo.mark_consumed(db, pairing, node.id)
    await db.commit()
    return AiNodePairResult(
        ai_node_token=create_ai_node_token(node.id),
        ai_node_id=node.id,
        config=_config(node),
    )


@router.post("/heartbeat", response_model=AiNodeConfig)
async def ai_node_heartbeat(
    body: AiNodeHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db)],
    node: Annotated[AiNode, Depends(get_current_ai_node)],
) -> AiNodeConfig:
    """Report telemetry + receive the latest config in one round-trip."""
    telemetry = json.dumps(body.model_dump(exclude_none=True))
    if body.version and body.version != node.version:
        node.version = body.version
    await ai_node_repo.touch_heartbeat(db, node, telemetry)
    await db.commit()
    return _config(node)


@router.get("/config", response_model=AiNodeConfig)
async def ai_node_config(
    node: Annotated[AiNode, Depends(get_current_ai_node)],
) -> AiNodeConfig:
    return _config(node)
