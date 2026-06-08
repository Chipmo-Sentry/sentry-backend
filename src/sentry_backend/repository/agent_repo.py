"""Agent + pairing-code persistence."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent, AgentPairingCode

PAIRING_CODE_TTL_MINUTES = 10
_CODE_DIGITS = 6


def _now() -> datetime:
    return datetime.now(UTC)


def _gen_code() -> str:
    """Zero-padded 6-digit numeric code (000000–999999)."""
    return f"{secrets.randbelow(10**_CODE_DIGITS):0{_CODE_DIGITS}d}"


async def create_pairing_code(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    created_by_user_id: UUID | None,
) -> AgentPairingCode:
    """Create a fresh, unconsumed pairing code. Retries on the (rare) chance a
    still-active code collides."""
    expires_at = _now() + timedelta(minutes=PAIRING_CODE_TTL_MINUTES)
    for _ in range(10):
        code = _gen_code()
        if await _active_code(db, code) is None:
            row = AgentPairingCode(
                code=code,
                organization_id=organization_id,
                store_id=store_id,
                created_by_user_id=created_by_user_id,
                expires_at=expires_at,
            )
            db.add(row)
            await db.flush()
            return row
    raise RuntimeError("could not allocate a unique pairing code")


async def _active_code(db: AsyncSession, code: str) -> AgentPairingCode | None:
    result = await db.execute(
        select(AgentPairingCode).where(
            AgentPairingCode.code == code,
            AgentPairingCode.consumed_at.is_(None),
            AgentPairingCode.expires_at > _now(),
        )
    )
    return result.scalar_one_or_none()


async def consume_pairing_code(db: AsyncSession, code: str) -> AgentPairingCode | None:
    """Lock + return the active code row (caller marks it consumed).

    Uses SELECT … FOR UPDATE so two concurrent pair requests with the same code
    serialize: the second blocks until the first commits, then the predicate
    (consumed_at IS NULL) no longer matches and it returns None. Closes the
    single-use TOCTOU. (On SQLite the lock is a no-op; tests run serially.)
    """
    result = await db.execute(
        select(AgentPairingCode)
        .where(
            AgentPairingCode.code == code,
            AgentPairingCode.consumed_at.is_(None),
            AgentPairingCode.expires_at > _now(),
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def create_agent(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID,
    name: str | None,
    paired_by_user_id: UUID | None,
) -> Agent:
    agent = Agent(
        organization_id=organization_id,
        store_id=store_id,
        name=name,
        paired_by_user_id=paired_by_user_id,
        last_seen_at=_now(),
    )
    db.add(agent)
    await db.flush()
    return agent


async def get_agent_by_name(db: AsyncSession, store_id: UUID, name: str) -> Agent | None:
    """The store's existing agent with this name (= the PC's hostname), if any.

    Lets re-pairing the SAME computer REUSE its row instead of creating a
    duplicate "connected computer". Prefers the most-recently-seen match.
    """
    result = await db.execute(
        select(Agent)
        .where(Agent.store_id == store_id, Agent.name == name)
        .order_by(Agent.last_seen_at.desc().nullslast(), Agent.created_at.desc())
    )
    return result.scalars().first()


async def reactivate_agent(db: AsyncSession, agent: Agent) -> None:
    """A known computer paired again — bring it back online (token is reissued
    by the caller), don't make a second row."""
    agent.is_active = True
    agent.last_seen_at = _now()
    await db.flush()


async def mark_consumed(db: AsyncSession, pairing: AgentPairingCode, agent_id: UUID) -> None:
    pairing.consumed_at = _now()
    pairing.agent_id = agent_id
    await db.flush()


async def get_agent(db: AsyncSession, agent_id: UUID) -> Agent | None:
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


async def list_agents_for_store(db: AsyncSession, store_id: UUID) -> list[Agent]:
    result = await db.execute(
        select(Agent).where(Agent.store_id == store_id).order_by(Agent.created_at.desc())
    )
    return list(result.scalars().all())


async def touch_last_seen(db: AsyncSession, agent: Agent) -> None:
    agent.last_seen_at = _now()
    await db.flush()


async def deactivate_agent(db: AsyncSession, agent: Agent) -> None:
    agent.is_active = False
    await db.flush()


async def delete_agent(db: AsyncSession, agent: Agent) -> None:
    """Hard-delete a paired computer. Pairing codes referencing it have their
    used_by_agent_id set to NULL via the FK's ON DELETE SET NULL."""
    await db.delete(agent)
    await db.flush()
