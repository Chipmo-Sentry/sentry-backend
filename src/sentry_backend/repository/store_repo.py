"""Store CRUD."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.organization import Organization
from sentry_backend.db.models.store import Store


async def list_all_with_org_and_camera_count(
    db: AsyncSession,
) -> list[tuple[Store, str, int]]:
    """Every store across ALL orgs with (store, organization_name, camera_count),
    ordered by org then store name. Super-admin store pickers (e.g. the per-store
    edge-config editor) use this. Cross-org — gate at the route."""
    cam_counts = (
        select(Camera.store_id, func.count(Camera.id).label("n"))
        .group_by(Camera.store_id)
        .subquery()
    )
    result = await db.execute(
        select(Store, Organization.name, func.coalesce(cam_counts.c.n, 0))
        .join(Organization, Organization.id == Store.organization_id)
        .outerjoin(cam_counts, cam_counts.c.store_id == Store.id)
        .order_by(Organization.name, Store.name)
    )
    return [(store, org_name, int(cam_n)) for store, org_name, cam_n in result.all()]


async def list_stores_for_org(db: AsyncSession, org_id: UUID) -> list[Store]:
    result = await db.execute(
        select(Store).where(Store.organization_id == org_id).order_by(Store.name)
    )
    return list(result.scalars().all())


async def get_store(db: AsyncSession, store_id: UUID, org_id: UUID) -> Store | None:
    result = await db.execute(
        select(Store).where(
            Store.id == store_id,
            Store.organization_id == org_id,
        )
    )
    return result.scalar_one_or_none()


async def create_store(
    db: AsyncSession,
    *,
    organization_id: UUID,
    name: str,
    address: str | None,
    timezone: str,
    telegram_chat_id: str | None = None,
) -> Store:
    store = Store(
        organization_id=organization_id,
        name=name,
        address=address,
        timezone=timezone,
        telegram_chat_id=telegram_chat_id or None,
    )
    db.add(store)
    await db.flush()
    return store


async def update_store(
    db: AsyncSession,
    store: Store,
    *,
    name: str | None = None,
    address: str | None = None,
    timezone: str | None = None,
    telegram_chat_id: str | None = None,
) -> Store:
    if name is not None:
        store.name = name
    if address is not None:
        store.address = address
    if timezone is not None:
        store.timezone = timezone
    if telegram_chat_id is not None:
        # Empty string clears the override (falls back to the global chat).
        store.telegram_chat_id = telegram_chat_id or None
    await db.flush()
    return store


async def delete_store(db: AsyncSession, store: Store) -> None:
    await db.delete(store)
