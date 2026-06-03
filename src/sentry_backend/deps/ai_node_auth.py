"""AI-node-token authentication dependency (typ=ai_node JWT, Bearer header)."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.ai_node import AiNode
from sentry_backend.deps.db import get_db
from sentry_backend.repository import ai_node_repo
from sentry_backend.security import decode_user_token

AI_NODE_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid AI node credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_ai_node(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> AiNode:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AI_NODE_CREDENTIALS_EXCEPTION
    token = authorization.split(maxsplit=1)[1]
    try:
        payload = decode_user_token(token)
    except ValueError as e:
        raise AI_NODE_CREDENTIALS_EXCEPTION from e
    if payload.get("typ") != "ai_node":
        raise AI_NODE_CREDENTIALS_EXCEPTION
    try:
        node_id = UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise AI_NODE_CREDENTIALS_EXCEPTION from e
    node = await ai_node_repo.get_node(db, node_id)
    if node is None or not node.is_active:
        raise AI_NODE_CREDENTIALS_EXCEPTION
    return node
