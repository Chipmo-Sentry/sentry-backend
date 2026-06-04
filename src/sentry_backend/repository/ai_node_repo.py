"""AI node + pairing-code persistence (mirrors agent_repo, but global)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.ai_node import AiNode, AiNodeMetric, AiNodePairingCode

# Numeric columns averaged when the metrics time-series is bucketed by hour.
_METRIC_COLS = (
    "cpu_pct",
    "ram_used_mb",
    "ram_total_mb",
    "gpu_pct",
    "vram_used_mb",
    "vram_total_mb",
    "gpu_temp_c",
    "fps_inference",
    "active_cameras",
)

_CODE_DIGITS = 6
PAIRING_CODE_TTL_MINUTES = 30


def _now() -> datetime:
    return datetime.now(UTC)


def _gen_code() -> str:
    import secrets

    return f"{secrets.randbelow(10**_CODE_DIGITS):0{_CODE_DIGITS}d}"


async def _active_code(db: AsyncSession, code: str) -> AiNodePairingCode | None:
    result = await db.execute(
        select(AiNodePairingCode).where(
            AiNodePairingCode.code == code,
            AiNodePairingCode.consumed_at.is_(None),
            AiNodePairingCode.expires_at > _now(),
        )
    )
    return result.scalar_one_or_none()


async def create_pairing_code(
    db: AsyncSession, *, created_by_user_id: UUID | None
) -> AiNodePairingCode:
    expires_at = _now() + timedelta(minutes=PAIRING_CODE_TTL_MINUTES)
    for _ in range(10):
        code = _gen_code()
        if await _active_code(db, code) is None:
            row = AiNodePairingCode(
                code=code,
                created_by_user_id=created_by_user_id,
                expires_at=expires_at,
            )
            db.add(row)
            await db.flush()
            return row
    raise RuntimeError("could not allocate a unique pairing code")


async def consume_pairing_code(db: AsyncSession, code: str) -> AiNodePairingCode | None:
    return await _active_code(db, code)


async def mark_consumed(db: AsyncSession, pairing: AiNodePairingCode, ai_node_id: UUID) -> None:
    pairing.consumed_at = _now()
    pairing.ai_node_id = ai_node_id
    await db.flush()


async def create_node(
    db: AsyncSession,
    *,
    name: str | None,
    hostname: str | None,
    public_url: str | None,
    version: str | None,
    gpu: str | None,
    paired_by_user_id: UUID | None,
) -> AiNode:
    node = AiNode(
        name=name,
        hostname=hostname,
        public_url=public_url,
        version=version,
        gpu=gpu,
        paired_by_user_id=paired_by_user_id,
    )
    db.add(node)
    await db.flush()
    return node


async def get_node(db: AsyncSession, node_id: UUID) -> AiNode | None:
    return (await db.execute(select(AiNode).where(AiNode.id == node_id))).scalar_one_or_none()


async def list_nodes(db: AsyncSession) -> list[AiNode]:
    result = await db.execute(select(AiNode).order_by(AiNode.created_at.desc()))
    return list(result.scalars().all())


async def list_active_nodes(db: AsyncSession) -> list[AiNode]:
    result = await db.execute(
        select(AiNode)
        .where(AiNode.is_active.is_(True), AiNode.enabled.is_(True))
        .order_by(AiNode.last_seen_at.desc().nullslast())
    )
    return list(result.scalars().all())


async def touch_heartbeat(db: AsyncSession, node: AiNode, telemetry: str | None) -> None:
    node.last_seen_at = _now()
    if telemetry is not None:
        node.telemetry = telemetry
    await db.flush()


async def insert_metric(db: AsyncSession, node_id: UUID, values: dict[str, object]) -> None:
    """Append one resource sample to the time-series (docs/19). Best-effort —
    only the known numeric columns are taken from the heartbeat body."""
    row = AiNodeMetric(
        ai_node_id=node_id,
        ts=_now(),
        **{k: values.get(k) for k in _METRIC_COLS},
    )
    db.add(row)
    await db.flush()


async def get_metrics(
    db: AsyncSession,
    node_id: UUID,
    *,
    frm: datetime,
    to: datetime,
    bucket: str = "raw",
) -> list[dict[str, object]]:
    """Resource samples for `node_id` in [frm, to]. bucket='hour' averages each
    metric per hour (for wide ranges like 7d); 'raw' returns every sample."""
    if bucket == "hour":
        ts_col = func.date_trunc("hour", AiNodeMetric.ts).label("ts")
        cols = [ts_col] + [func.avg(getattr(AiNodeMetric, c)).label(c) for c in _METRIC_COLS]
        stmt = (
            select(*cols)
            .where(
                AiNodeMetric.ai_node_id == node_id,
                AiNodeMetric.ts >= frm,
                AiNodeMetric.ts <= to,
            )
            .group_by(ts_col)
            .order_by(ts_col)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "ts": r.ts.isoformat(),
                **{
                    c: (round(float(v), 1) if (v := getattr(r, c)) is not None else None)
                    for c in _METRIC_COLS
                },
            }
            for r in rows
        ]

    stmt = (
        select(AiNodeMetric)
        .where(
            AiNodeMetric.ai_node_id == node_id,
            AiNodeMetric.ts >= frm,
            AiNodeMetric.ts <= to,
        )
        .order_by(AiNodeMetric.ts)
    )
    samples = (await db.execute(stmt)).scalars().all()
    return [{"ts": s.ts.isoformat(), **{c: getattr(s, c) for c in _METRIC_COLS}} for s in samples]


async def deactivate_node(db: AsyncSession, node: AiNode) -> None:
    node.is_active = False
    await db.flush()


async def update_node(
    db: AsyncSession,
    node: AiNode,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    provider: str | None = None,
    frame_skip: int | None = None,
) -> AiNode:
    if name is not None:
        node.name = name
    if enabled is not None:
        node.enabled = enabled
    if provider is not None:
        node.provider = provider
    if frame_skip is not None:
        node.frame_skip = frame_skip
    await db.flush()
    return node
