"""Founder-approved pricing (T14) — the ONLY place price numbers live.

Per-store, tiered by that store's ACTIVE (enabled) camera count:

* Starter    (1–9 cams):  49,000₮/month platform + 22,000₮/cam/month
* Pro        (10–50):     39,000₮ + 17,000₮/cam
* Enterprise (51+):       29,000₮ + 12,000₮/cam

A store with ZERO enabled cameras costs nothing (no platform fee either —
платформ төлбөр зөвхөн камертай дэлгүүрт). The daily charge is the monthly
total ÷ 30, rounded half-up per store, summed across the org's stores.

Everything here is pure (no DB, no clock) so it is unit-tested directly.
"""

from collections.abc import Iterable
from dataclasses import dataclass

DAYS_PER_MONTH = 30


@dataclass(frozen=True)
class Tier:
    name: str
    min_cameras: int
    max_cameras: int | None  # None = unbounded
    platform_fee_mnt: int
    camera_fee_mnt: int


STARTER = Tier("starter", 1, 9, 49_000, 22_000)
PRO = Tier("pro", 10, 50, 39_000, 17_000)
ENTERPRISE = Tier("enterprise", 51, None, 29_000, 12_000)

TIERS: tuple[Tier, ...] = (STARTER, PRO, ENTERPRISE)


def tier_for(active_cameras: int) -> Tier:
    """Tier a store falls into by its active camera count (must be >= 1)."""
    if active_cameras < 1:
        raise ValueError("tier_for requires at least 1 active camera")
    for tier in TIERS:
        if tier.max_cameras is None or active_cameras <= tier.max_cameras:
            return tier
    raise AssertionError("unreachable: ENTERPRISE has no upper bound")


def store_monthly_mnt(active_cameras: int) -> int:
    """Monthly fee for ONE store. Zero cameras → zero (no platform fee)."""
    if active_cameras <= 0:
        return 0
    tier = tier_for(active_cameras)
    return tier.platform_fee_mnt + tier.camera_fee_mnt * active_cameras


def daily_from_monthly(monthly_mnt: int) -> int:
    """Monthly → daily: ÷30, rounded half-up to a whole ₮."""
    if monthly_mnt <= 0:
        return 0
    return (monthly_mnt + DAYS_PER_MONTH // 2) // DAYS_PER_MONTH


def org_daily_charge_mnt(store_camera_counts: Iterable[int]) -> int:
    """Org's total daily charge: per-store monthly → daily (rounded), summed."""
    return sum(daily_from_monthly(store_monthly_mnt(n)) for n in store_camera_counts)


def org_monthly_charge_mnt(store_camera_counts: Iterable[int]) -> int:
    """Org's total monthly rate (display only — billing runs daily)."""
    return sum(store_monthly_mnt(n) for n in store_camera_counts)
