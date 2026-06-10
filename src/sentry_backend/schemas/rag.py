"""RAG verified-case schemas (docs/19 Phase 4)."""

from uuid import UUID

from pydantic import BaseModel, Field


class RagCaseCreate(BaseModel):
    """Store a verified event the AI node has embedded."""

    store_id: UUID | None = None
    verdict: str = Field(max_length=32)
    category: str | None = Field(default=None, max_length=32)
    description: str = Field(min_length=1, max_length=8000)
    embedding: list[float] = Field(min_length=1, max_length=4096)


class RagSimilarRequest(BaseModel):
    """Ask for the most similar past verified cases."""

    store_id: UUID | None = None
    embedding: list[float] = Field(min_length=1, max_length=4096)
    k: int = Field(default=3, ge=1, le=10)


class RagCaseMatch(BaseModel):
    description: str
    category: str | None
    verdict: str
    score: float
