"""Platform Telegram bot-token persistence (superadmin-configurable).

The Telegram bot is ONE platform-wide integration: a single bot token (set by the
superadmin) delivers alerts to each store's own ``Store.telegram_chat_id``. The
token is a secret, so it is Fernet-encrypted at rest in the shared ``app_config``
table under ``key='telegram'``. ``settings.telegram_bot_token`` (env) stays a
FALLBACK, so a deploy that already sets the env var keeps working until a token
is saved in the superadmin UI (the DB value then wins).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.app_config import AppConfig
from sentry_backend.logging_setup import get_logger
from sentry_backend.security import decrypt_secret, encrypt_secret

log = get_logger("sentry_backend.telegram_config")

TELEGRAM_KEY = "telegram"


async def _row(db: AsyncSession) -> AppConfig | None:
    result = await db.execute(select(AppConfig).where(AppConfig.key == TELEGRAM_KEY))
    return result.scalar_one_or_none()


async def get_bot_token(db: AsyncSession) -> str | None:
    """The decrypted platform bot token, or None when unset. Callers fall back to
    the env token. A corrupt value (e.g. the Fernet key was rotated) returns None
    rather than raising, so a notification never crashes the alert flow."""
    row = await _row(db)
    if row is None:
        return None
    enc = (row.value or {}).get("bot_token_enc")
    if not enc:
        return None
    try:
        return decrypt_secret(str(enc).encode())
    except Exception:  # noqa: BLE001 — corrupt/rotated-key ciphertext
        log.warning("telegram_token_decrypt_failed")
        return None


async def get_status(db: AsyncSession) -> tuple[bool, str | None]:
    """(configured, token_hint) for the superadmin read view. The hint is the
    last 4 chars only — the full secret is NEVER returned to the client."""
    token = await get_bot_token(db)
    if not token:
        return False, None
    hint = f"…{token[-4:]}" if len(token) >= 4 else "…"
    return True, hint


async def set_token(db: AsyncSession, token: str | None) -> None:
    """Upsert the bot token. A non-empty ``token`` is encrypted + stored; an empty
    / None ``token`` clears it (so the superadmin can remove the integration)."""
    row = await _row(db)
    value = dict((row.value if row else None) or {})
    if token:
        value["bot_token_enc"] = encrypt_secret(token).decode()
    else:
        value.pop("bot_token_enc", None)
    if row is None:
        db.add(AppConfig(key=TELEGRAM_KEY, value=value))
    else:
        row.value = value
    await db.flush()
