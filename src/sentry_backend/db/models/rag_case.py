"""Verified-case store for the RAG feedback loop (docs/19 Phase 4).

When staff confirm/deny a suspicion alert, the event (a short text description
of what the VLM saw + the staff verdict) is kept here with its embedding. On a
NEW verification the AI node retrieves the most similar past cases for the same
store and feeds them to the VLM as few-shot context — so judgments drift toward
each store's real patterns over time, WITHOUT retraining the model.

Embeddings are stored as a plain float array (model-agnostic length) and ranked
by cosine similarity in Python — no pgvector extension needed at pilot scale.
"""

from uuid import UUID

from sqlalchemy import ARRAY, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VerifiedCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "verified_cases"

    # Scope retrieval per store (each store learns its own patterns). Null = a
    # global case usable as a fallback.
    store_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stores.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    # Verdict the staff gave: "true_positive" | "false_positive" | "unclear".
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    # VLM behaviour category (browsing / cart_pickup / pocket_conceal / other).
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Short natural-language description of the event (what the VLM saw).
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Embedding of `description` (length = the embed model's dim; e.g. 768).
    embedding: Mapped[list[float]] = mapped_column(ARRAY(Float), nullable=False)
