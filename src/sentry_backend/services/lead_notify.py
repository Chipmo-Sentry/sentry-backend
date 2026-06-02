"""Founder notification for new landing-page leads.

Best-effort: when a Telegram bot token + chat id are configured, a new demo
request pings the founder instantly. Otherwise it just logs (so leads are never
lost — they're persisted in the DB regardless). Never raises into the request
path: a notification failure must not fail the user's form submission.
"""

import httpx

from sentry_backend.db.models.lead import Lead
from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.lead_notify")


def _format(lead: Lead) -> str:
    lines = [
        "🟠 *Шинэ demo хүсэлт — Chipmo Sentry*",
        f"✉️ {lead.email}",
    ]
    if lead.name:
        lines.append(f"👤 {lead.name}")
    if lead.organization:
        lines.append(f"🏢 {lead.organization}")
    if lead.phone:
        lines.append(f"📞 {lead.phone}")
    if lead.industry:
        lines.append(f"🏷 {lead.industry}")
    if lead.camera_count:
        lines.append(f"📹 {lead.camera_count} камер")
    lines.append(f"🌐 {lead.source}")
    return "\n".join(lines)


async def notify_new_lead(lead: Lead) -> None:
    """Fire-and-forget founder ping. Swallows all errors."""
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        log.info("lead_received_no_notifier", email=lead.email, source=lead.source)
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": _format(lead),
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — notification must never break the request
        log.warning("lead_notify_failed", email=lead.email, exc_info=True)
