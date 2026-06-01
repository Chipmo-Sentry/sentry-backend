"""Generic key-value config table (currently used by behavior tuning)."""

from typing import Any

from sqlalchemy import String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import sqltypes

from sentry_backend.db.base import Base, TimestampMixin


class AppConfig(TimestampMixin, Base):
    """Single row per `key`; `value` is arbitrary JSONB.

    M1 use cases: `behavior` (global behavior weights + thresholds).
    M2 will likely move behavior config to per-camera rows on `cameras`;
    this table stays for app-wide flags (feature toggles etc.).
    """

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at_db: Mapped[sqltypes.DateTime] = mapped_column(
        sqltypes.DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
