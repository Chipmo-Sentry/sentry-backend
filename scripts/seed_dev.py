"""Seed a fresh dev DB with a super-admin user, an org, a membership, and a store.

Usage:
    uv run python scripts/seed_dev.py

Reads DATABASE_URL from .env. Idempotent — re-running prints "already exists"
for each row that's already there.

After seeding, login with:
    email:    admin@chipmo.mn
    password: ChipmoDev2026!
"""

import asyncio

from sqlalchemy import select

from sentry_backend.db.models.organization import (
    Organization,
    OrganizationMember,
    OrgRole,
)
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.db.session import dispose_engine, session_scope
from sentry_backend.security import hash_password

SEED_EMAIL = "admin@chipmo.mn"
SEED_PASSWORD = "ChipmoDev2026!"  # noqa: S105 — dev only
SEED_ORG_NAME = "Demo Retail Group"
SEED_ORG_SLUG = "demo-retail"
SEED_STORE_NAME = "Demo Store #1"


async def main() -> None:
    async with session_scope() as db:
        # User
        user = (await db.execute(select(User).where(User.email == SEED_EMAIL))).scalar_one_or_none()
        if user is None:
            user = User(
                email=SEED_EMAIL,
                hashed_password=hash_password(SEED_PASSWORD),
                is_super_admin=True,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            print(f"✅ Created user {SEED_EMAIL} (id={user.id})")
        else:
            print(f"ℹ️  User {SEED_EMAIL} already exists (id={user.id})")

        # Org
        org = (
            await db.execute(select(Organization).where(Organization.slug == SEED_ORG_SLUG))
        ).scalar_one_or_none()
        if org is None:
            org = Organization(name=SEED_ORG_NAME, slug=SEED_ORG_SLUG)
            db.add(org)
            await db.flush()
            print(f"✅ Created org '{SEED_ORG_NAME}' (id={org.id})")
        else:
            print(f"ℹ️  Org '{SEED_ORG_NAME}' already exists (id={org.id})")

        # Membership
        member = (
            await db.execute(
                select(OrganizationMember).where(
                    OrganizationMember.user_id == user.id,
                    OrganizationMember.organization_id == org.id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            db.add(
                OrganizationMember(
                    user_id=user.id,
                    organization_id=org.id,
                    role=OrgRole.owner,
                )
            )
            await db.flush()
            print(f"✅ Granted {SEED_EMAIL} owner of '{SEED_ORG_NAME}'")
        else:
            print("ℹ️  Membership already exists")

        # Store
        store = (
            await db.execute(
                select(Store).where(
                    Store.organization_id == org.id,
                    Store.name == SEED_STORE_NAME,
                )
            )
        ).scalar_one_or_none()
        if store is None:
            store = Store(
                organization_id=org.id,
                name=SEED_STORE_NAME,
                address="Ulaanbaatar, Sukhbaatar district",
                timezone="Asia/Ulaanbaatar",
            )
            db.add(store)
            await db.flush()
            print(f"✅ Created store '{SEED_STORE_NAME}' (id={store.id})")
        else:
            print(f"ℹ️  Store '{SEED_STORE_NAME}' already exists (id={store.id})")

    await dispose_engine()
    print()
    print("Seed complete. Login at http://localhost:3000/login")
    print(f"  email:    {SEED_EMAIL}")
    print(f"  password: {SEED_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
