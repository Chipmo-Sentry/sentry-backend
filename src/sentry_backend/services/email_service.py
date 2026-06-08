"""Transactional email via SMTP (org user invites).

Best-effort: when SMTP isn't configured (no smtp_host/smtp_from), send_email()
returns False and the caller surfaces the invite link in the API response so an
admin can still share it manually — invites never block on email infrastructure.
Blocking smtplib runs in a thread so it doesn't stall the event loop.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.email")


def is_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_from)


async def send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """Send one email. Returns True on success, False if unconfigured or failed."""
    s = get_settings()
    if not (s.smtp_host and s.smtp_from):
        log.info("email.skip_unconfigured", to=to)
        return False

    def _send() -> None:
        msg = EmailMessage()
        msg["From"] = s.smtp_from or ""
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        pw = s.smtp_password.get_secret_value() if s.smtp_password else ""
        ctx = ssl.create_default_context()
        if s.smtp_ssl:
            with smtplib.SMTP_SSL(s.smtp_host or "", s.smtp_port, context=ctx, timeout=20) as srv:
                if s.smtp_user:
                    srv.login(s.smtp_user, pw)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(s.smtp_host or "", s.smtp_port, timeout=20) as srv:
                srv.starttls(context=ctx)
                if s.smtp_user:
                    srv.login(s.smtp_user, pw)
                srv.send_message(msg)

    try:
        await asyncio.to_thread(_send)
        log.info("email.sent", to=to, subject=subject)
        return True
    except Exception as e:  # noqa: BLE001 — email must never crash the request
        log.warning("email.send_failed", to=to, error=str(e))
        return False
