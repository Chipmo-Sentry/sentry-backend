"""Agent-token authentication dependency.

Paired agents authenticate with a long-lived ``typ=agent`` JWT (Bearer header).
The token carries only the agent id; org/store are read from the Agent row so a
revoked or reassigned agent can't act on stale claims.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent
from sentry_backend.deps.db import get_db
from sentry_backend.repository import agent_repo
from sentry_backend.security import decode_user_token

AGENT_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid agent credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_agent(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Agent:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AGENT_CREDENTIALS_EXCEPTION
    token = authorization.split(maxsplit=1)[1]

    try:
        payload = decode_user_token(token)
    except ValueError as e:
        raise AGENT_CREDENTIALS_EXCEPTION from e

    if payload.get("typ") != "agent":
        raise AGENT_CREDENTIALS_EXCEPTION
    try:
        agent_id = UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise AGENT_CREDENTIALS_EXCEPTION from e

    agent = await agent_repo.get_agent(db, agent_id)
    if agent is None or not agent.is_active:
        raise AGENT_CREDENTIALS_EXCEPTION
    return agent
