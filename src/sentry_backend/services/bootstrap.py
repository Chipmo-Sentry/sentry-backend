"""Idempotent super-admin bootstrap from environment.

Lets a fresh deployment (e.g. Railway) seed its first super-admin without shell
access or exposing the DB. If BOOTSTRAP_SUPERADMIN_EMAIL + _PASSWORD are set and
no super-admin exists yet, create the user + a default org + owner membership.
Safe to leave configured — it no-ops once the user exists.
"""

from sqlalchemy import select

from sentry_backend.db.models.organization import (
    Organization,
    OrganizationMember,
    OrgRole,
)
from sentry_backend.db.models.user import User
from sentry_backend.db.session import session_scope
from sentry_backend.logging_setup import get_logger
from sentry_backend.security import hash_password
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.bootstrap")


async def bootstrap_superadmin() -> None:
    settings = get_settings()
    email = settings.bootstrap_superadmin_email
    password = settings.bootstrap_superadmin_password
    if not email or not password:
        return

    async with session_scope() as db:
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is not None:
            log.info("bootstrap_superadmin_exists", email=email)
            return

        user = User(
            email=email,
            hashed_password=hash_password(password.get_secret_value()),
            is_super_admin=True,
            is_active=True,
        )
        db.add(user)
        await db.flush()

        org = (
            await db.execute(
                select(Organization).where(Organization.slug == settings.bootstrap_org_slug)
            )
        ).scalar_one_or_none()
        if org is None:
            org = Organization(
                name=settings.bootstrap_org_name,
                slug=settings.bootstrap_org_slug,
            )
            db.add(org)
            await db.flush()

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

    log.info("bootstrap_superadmin_created", email=email, org=settings.bootstrap_org_slug)
