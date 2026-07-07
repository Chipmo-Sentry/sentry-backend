"""Alert → Telegram notification (BE1).

Pings store staff (or a global ops chat) when an *actionable* alert fires —
live-threshold breaches and clip-verified suspicions. Best-effort: a failure
here never breaks alert creation or the SSE publish.

Chat resolution order: the alert's store `telegram_chat_id` → the global
`TELEGRAM_ALERT_CHAT_ID` → none (skip). Reuses `TELEGRAM_BOT_TOKEN`.
"""

import asyncio
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.alert import Alert, AlertCategory, AlertLevel
from sentry_backend.db.models.clip import Clip
from sentry_backend.db.models.store import Store
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import telegram_config_repo
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.alert_notify")

# Telegram bots may upload a video up to 50 MB via sendVideo. Our edge clips are
# ~[-3s..+3s] (well under this), but guard anyway: an oversized clip falls back
# to a text-only ping rather than a failed upload.
_TELEGRAM_VIDEO_MAX_BYTES = 50 * 1024 * 1024

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
            await db.execute(select(Store.telegram_chat_id).where(Store.id == alert.store_id))
        ).scalar_one_or_none()
        if chat:
            return chat
    return settings.telegram_alert_chat_id


def _probe_clip(storage_path: str | None) -> Path | None:
    """Sync disk check (runs in a thread): the path exists and is small enough
    to send as a Telegram video. None → caller sends text only."""
    if not storage_path:
        return None
    path = Path(storage_path)
    try:
        if not path.is_file() or path.stat().st_size > _TELEGRAM_VIDEO_MAX_BYTES:
            return None
    except OSError:
        return None
    return path


async def _resolve_clip_path(db: AsyncSession, alert: Alert) -> Path | None:
    """The evidence-clip file for this alert, if present + small enough to send
    as a video. The blocking disk stat is offloaded to a thread."""
    if alert.clip_id is None:
        return None
    storage_path = (
        await db.execute(select(Clip.storage_path).where(Clip.id == alert.clip_id))
    ).scalar_one_or_none()
    return await asyncio.to_thread(_probe_clip, storage_path)


async def notify_alert(db: AsyncSession, alert: Alert) -> None:
    """Fire-and-forget Telegram ping for an actionable alert — with the evidence
    CLIP attached as a video when we have one, else a text-only message. Swallows
    errors: a notification failure must never break alert creation."""
    if not is_actionable(alert.alert_level):
        return
    # Token resolution: the superadmin-set DB value wins; the env var is a
    # fallback so an existing env-based deploy keeps working (I2 / superadmin UI).
    settings = get_settings()
    token = await telegram_config_repo.get_bot_token(db)
    if not token and settings.telegram_bot_token:
        token = settings.telegram_bot_token.get_secret_value()
    if not token:
        return
    chat_id = await _resolve_chat_id(db, alert)
    if not chat_id:
        log.info("alert_notify_no_chat", alert_id=str(alert.id))
        return

    api = f"https://api.telegram.org/bot{token}"
    text = _format(alert)
    clip_path = await _resolve_clip_path(db, alert)
    try:
        # 20s: a video upload takes longer than a text ping.
        async with httpx.AsyncClient(timeout=20.0) as client:
            if clip_path is not None:
                # sendVideo with the clip as multipart; the alert summary rides
                # along as the caption so staff see the verdict + the footage in
                # one message. A telegram-side failure falls through to text.
                # Read the file in a thread — it's <=50 MB and the event loop
                # must not block on disk.
                video_bytes = await asyncio.to_thread(clip_path.read_bytes)
                resp = await client.post(
                    f"{api}/sendVideo",
                    data={
                        "chat_id": chat_id,
                        "caption": text[:1024],  # Telegram caption cap
                        "supports_streaming": "true",
                    },
                    files={"video": (clip_path.name, video_bytes, "video/mp4")},
                )
                if resp.is_success:
                    return
                log.warning(
                    "alert_notify_video_failed",
                    alert_id=str(alert.id),
                    status=resp.status_code,
                )
            # No clip, or the video upload failed → text-only fallback.
            resp = await client.post(
                f"{api}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — notification must never break alert flow
        log.warning("alert_notify_failed", alert_id=str(alert.id), exc_info=True)
