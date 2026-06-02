"""Alert → Telegram notification (BE1).

Pings store staff (or a global ops chat) when an *actionable* alert fires —
live-threshold breaches and clip-verified suspicions. Best-effort: a failure
here never breaks alert creation or the SSE publish.

Chat resolution order: the alert's store `telegram_chat_id` → the global
`TELEGRAM_ALERT_CHAT_ID` → none (skip). Reuses `TELEGRAM_BOT_TOKEN`.
"""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.alert import Alert, AlertCategory, AlertLevel
from sentry_backend.db.models.store import Store
from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.alert_notify")

_CATEGORY_LABEL: dict[AlertCategory, str] = {
    AlertCategory.browsing: "Хайж байгаа",
    AlertCategory.cart_pickup: "Сагсанд авсан",
    AlertCategory.pocket_conceal: "Халаасанд хийсэн",
    AlertCategory.other: "Бусад",
}

_LEVEL_LABEL: dict[AlertLevel, str] = {
    AlertLevel.ignore: "Үл хамаа",
    AlertLevel.log: "Бүртгэсэн",
    AlertLevel.notify: "Анхаар",
    AlertLevel.review: "Шалга",
}


def is_actionable(level: AlertLevel) -> bool:
    """Only notify on alerts worth a human's attention."""
    return level in (AlertLevel.notify, AlertLevel.review)


def _format(alert: Alert) -> str:
    is_breach = alert.triggered_by.value == "live_threshold"
    head = "🚨 Шууд сэрэмжлүүлэг" if is_breach else "🔔 Сэжигтэй үйлдэл"
    lines = [
        f"{head} — {_LEVEL_LABEL.get(alert.alert_level, alert.alert_level.value)}",
        f"📋 {_CATEGORY_LABEL.get(alert.category, alert.category.value)} · {round(alert.confidence * 100)}%",
    ]
    if is_breach and alert.person_id is not None:
        risk = f" · эрсдэл {alert.peak_risk_pct:.0f}" if alert.peak_risk_pct is not None else ""
        lines.append(f"👤 Хүн #{alert.person_id}{risk}")
    if alert.reasoning:
        lines.append(f"💬 {alert.reasoning[:300]}")
    return "\n".join(lines)


async def _resolve_chat_id(db: AsyncSession, alert: Alert) -> str | None:
    settings = get_settings()
    if alert.store_id is not None:
        chat = (
            await db.execute(
                select(Store.telegram_chat_id).where(Store.id == alert.store_id)
            )
        ).scalar_one_or_none()
        if chat:
            return chat
    return settings.telegram_alert_chat_id


async def notify_alert(db: AsyncSession, alert: Alert) -> None:
    """Fire-and-forget Telegram ping for an actionable alert. Swallows errors."""
    if not is_actionable(alert.alert_level):
        return
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return
    chat_id = await _resolve_chat_id(db, alert)
    if not chat_id:
        log.info("alert_notify_no_chat", alert_id=str(alert.id))
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": _format(alert),
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — notification must never break alert flow
        log.warning("alert_notify_failed", alert_id=str(alert.id), exc_info=True)
