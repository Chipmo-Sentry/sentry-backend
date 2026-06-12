"""Daily usage charger (T14) — background loop, same shape as retention.py.

Every `billing_charge_interval_sec` it wakes and, for each org that has NOT yet
been charged for TODAY (Ulaanbaatar calendar day — zoneinfo Asia/Ulaanbaatar):

1. counts enabled cameras per store (a store with zero cameras costs nothing),
2. computes the org's daily total via pricing.org_daily_charge_mnt,
3. posts ONE usage_charge journal entry (Dr org_wallet / Cr revenue) with
   charge_date = today, updating the wallet balance in the same transaction.

Suspended orgs are SKIPPED — service is gated, so no money runs ("үйлчилгээ
хаалттай үед төлбөр гүйхгүй"). Credit and promo orgs ARE charged; during an
open promo window an equal promo_credit entry is posted alongside, so the day
nets to zero but both the usage and the marketing cost stay visible in the
ledger.

Idempotency is owned by the DATABASE: the partial unique index on
(org_id, charge_date) WHERE kind='usage_charge'. Each org is processed in its
own transaction, so a concurrent replica racing on the same day just hits an
IntegrityError for that org and is skipped — never a double charge, never a
poisoned batch.
"""

import asyncio
from contextlib import suppress
from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.billing_charger")

ULAANBAATAR = ZoneInfo("Asia/Ulaanbaatar")


def ub_today(now: datetime) -> date:
    """The Ulaanbaatar calendar day `now` falls in (the charge_date key)."""
    return now.astimezone(ULAANBAATAR).date()


async def charge_once(now: datetime | None = None) -> dict[str, int]:
    """One charging pass over all orgs. Returns counters (logged)."""
    # Imports kept local so the pure helpers above stay importable without a DB
    # (mirrors retention.py / node_watchdog.py).
    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError

    from sentry_backend.db.models.billing import BillingJournal, JournalKind
    from sentry_backend.db.models.camera import Camera
    from sentry_backend.db.models.store import Store
    from sentry_backend.db.session import session_scope
    from sentry_backend.services.billing.ledger import get_or_create_profile, post_entry
    from sentry_backend.services.billing.pricing import org_daily_charge_mnt
    from sentry_backend.services.billing.status import BillingStatus, billing_status

    if now is None:
        now = datetime.now(UTC)
    today = ub_today(now)
    summary = {
        "charged": 0,
        "skipped_already_charged": 0,
        "skipped_suspended": 0,
        "skipped_no_cameras": 0,
        "skipped_conflict": 0,
        "promo_credited": 0,
    }

    # Snapshot: enabled-camera count per store, grouped to orgs in Python.
    async with session_scope() as db:
        rows = await db.execute(
            select(Store.organization_id, Store.id, func.count(Camera.id))
            .outerjoin(Camera, (Camera.store_id == Store.id) & Camera.enabled.is_(True))
            .group_by(Store.organization_id, Store.id)
        )
        org_store_counts: dict[UUID, list[int]] = {}
        for org_id, _store_id, cam_count in rows.all():
            org_store_counts.setdefault(org_id, []).append(int(cam_count))

        charged_today = set(
            (
                await db.execute(
                    select(BillingJournal.org_id).where(
                        BillingJournal.kind == JournalKind.usage_charge,
                        BillingJournal.charge_date == today,
                    )
                )
            )
            .scalars()
            .all()
        )

    for org_id, counts in org_store_counts.items():
        if org_id in charged_today:
            summary["skipped_already_charged"] += 1
            continue
        daily = org_daily_charge_mnt(counts)
        if daily <= 0:
            summary["skipped_no_cameras"] += 1
            continue
        # Per-org transaction: a unique-index race or any failure on one org
        # never blocks the others.
        try:
            async with session_scope() as db:
                profile = await get_or_create_profile(db, org_id)
                status = billing_status(
                    balance_mnt=profile.balance_mnt,
                    credit_until=profile.credit_until,
                    promo_free_until=profile.promo_free_until,
                    now=now,
                )
                if status is BillingStatus.suspended:
                    summary["skipped_suspended"] += 1
                    continue
                promo_open = profile.promo_free_until is not None and profile.promo_free_until > now
                await post_entry(
                    db,
                    org_id=org_id,
                    kind=JournalKind.usage_charge,
                    amount_mnt=daily,
                    description=f"Өдрийн хэрэглээ {today.isoformat()}",
                    meta={"store_camera_counts": counts},
                    charge_date=today,
                )
                if promo_open:
                    # Net the day to zero while keeping both the usage and the
                    # promo cost visible in the ledger.
                    await post_entry(
                        db,
                        org_id=org_id,
                        kind=JournalKind.promo_credit,
                        amount_mnt=daily,
                        description=f"Промо хөнгөлөлт {today.isoformat()}",
                        meta={"reason": "promo_free_window"},
                    )
                    summary["promo_credited"] += 1
                summary["charged"] += 1
        except IntegrityError:
            # Another worker charged this org for `today` between our snapshot
            # and the insert — the DB constraint did its job; just move on.
            summary["skipped_conflict"] += 1
            log.info("billing_charge_conflict", org_id=str(org_id), charge_date=str(today))

    log.info("billing_charge_done", charge_date=str(today), **summary)
    return summary


class BillingCharger:
    """Background loop started/stopped from the app lifespan (like RetentionSweeper)."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        settings = get_settings()
        if settings.billing_charge_interval_sec <= 0:
            log.info("billing_charger_disabled")
            return
        self._task = asyncio.create_task(self._run(), name="billing-charger")
        log.info("billing_charger_started", interval_sec=settings.billing_charge_interval_sec)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        interval = get_settings().billing_charge_interval_sec
        while True:
            try:
                await charge_once()
            except Exception:  # noqa: BLE001 — the loop must outlive any error
                log.warning("billing_charge_failed", exc_info=True)
            await asyncio.sleep(interval)
