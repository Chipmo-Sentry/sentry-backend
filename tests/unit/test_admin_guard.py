"""Unit tests for the super-admin self-lockout guard (pure logic, no DB)."""

from sentry_backend.schemas.admin import UserAdminUpdate, would_self_lockout

ACTOR = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def test_demoting_self_is_lockout() -> None:
    assert would_self_lockout(
        actor_id=ACTOR,
        target_id=ACTOR,
        update=UserAdminUpdate(is_super_admin=False),
    )


def test_deactivating_self_is_lockout() -> None:
    assert would_self_lockout(
        actor_id=ACTOR,
        target_id=ACTOR,
        update=UserAdminUpdate(is_active=False),
    )


def test_promoting_self_is_allowed() -> None:
    assert not would_self_lockout(
        actor_id=ACTOR,
        target_id=ACTOR,
        update=UserAdminUpdate(is_super_admin=True, is_active=True),
    )


def test_editing_other_user_is_never_lockout() -> None:
    assert not would_self_lockout(
        actor_id=ACTOR,
        target_id=OTHER,
        update=UserAdminUpdate(is_super_admin=False, is_active=False),
    )


def test_empty_update_is_not_lockout() -> None:
    assert not would_self_lockout(
        actor_id=ACTOR,
        target_id=ACTOR,
        update=UserAdminUpdate(),
    )
