"""Verified-case persistence + cosine-similarity retrieval (docs/19 Phase 4).

Pilot scale → we fetch a store's recent cases and rank in Python; no pgvector.
"""

from __future__ import annotations

import math
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.rag_case import VerifiedCase

# Cap the candidate set ranked per query — plenty for few-shot, bounds the work.
_CANDIDATE_LIMIT = 500


async def add_case(
    db: AsyncSession,
    *,
    store_id: UUID | None,
    verdict: str,
    category: str | None,
    description: str,
    embedding: list[float],
) -> VerifiedCase:
    case = VerifiedCase(
        store_id=store_id,
        verdict=verdict,
        category=category,
        description=description,
        embedding=embedding,
    )
    db.add(case)
    await db.flush()
    return case


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else -1.0


async def similar_cases(
    db: AsyncSession,
    *,
    store_id: UUID | None,
    embedding: list[float],
    k: int = 3,
) -> list[tuple[VerifiedCase, float]]:
    """Top-k cases (for this store, falling back to global) by cosine similarity
    to `embedding`, most similar first. Returns (case, score) pairs."""
    stmt = (
        select(VerifiedCase)
        .where(
            (VerifiedCase.store_id == store_id) | (VerifiedCase.store_id.is_(None))
        )
        .order_by(VerifiedCase.created_at.desc())
        .limit(_CANDIDATE_LIMIT)
    )
    candidates = list((await db.execute(stmt)).scalars().all())
    scored = [(c, _cosine(embedding, c.embedding)) for c in candidates]
    scored.sort(key=lambda cs: cs[1], reverse=True)
    return scored[: max(0, k)]
