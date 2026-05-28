"""Show recent live_threshold alerts (L5 debug)."""

import asyncio

from sqlalchemy import select

from sentry_backend.db.models.alert import Alert, AlertTrigger
from sentry_backend.db.session import dispose_engine, session_scope


async def main() -> None:
    async with session_scope() as db:
        r = await db.execute(
            select(Alert)
            .where(Alert.triggered_by == AlertTrigger.live_threshold)
            .order_by(Alert.created_at.desc())
            .limit(10),
        )
        rows = list(r.scalars())
    await dispose_engine()
    print(f"=== {len(rows)} live_threshold alerts (most recent first) ===")
    for a in rows:
        ts = a.created_at.strftime("%H:%M:%S")
        peak = f"{a.peak_risk_pct:.0f}%" if a.peak_risk_pct else "—"
        print(
            f"{ts}  pid={a.person_id:<4}  peak={peak:<5}  "
            f"cat={a.category.value:<14}  level={a.alert_level.value:<8}  "
            f"conf={a.confidence:.2f}  model={a.model_name}",
        )
        print(f"   reasoning: {a.reasoning[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
