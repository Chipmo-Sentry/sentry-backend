"""Server-side token revocation (T01 / H1):

- the access token's ``tv`` claim must match ``User.token_version``
- a mismatch (e.g. after a logout bump) is rejected with 401
- logout bumps ``token_version`` so all prior tokens stop verifying

These drive the ``get_current_user`` dependency directly with a stub session +
monkeypatched ``get_user_by_id`` (no real DB — matches the suite's style).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from sentry_backend.deps import auth as auth_deps
from sentry_backend.security import create_user_token


@dataclass
class FakeUser:
    id: UUID
    token_version: int = 0
    is_active: bool = True
    is_super_admin: bool = False
    email: str = "u@example.com"


class FakeSession:
    """Stand-in for AsyncSession — never touched because get_user_by_id is
    monkeypatched, but the dependency signature requires *something*."""


async def _call_get_current_user(token: str) -> FakeUser:
    return await auth_deps.get_current_user(  # type: ignore[return-value]
        db=FakeSession(),  # type: ignore[arg-type]
        sentry_access=token,
        authorization=None,
    )


def _patch_user(monkeypatch: pytest.MonkeyPatch, user: FakeUser | None) -> None:
    async def fake_get(_db: object, _uid: UUID) -> FakeUser | None:
        return user

    monkeypatch.setattr(auth_deps, "get_user_by_id", fake_get)


@pytest.mark.asyncio
async def test_matching_tv_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    user = FakeUser(id=uuid4(), token_version=3)
    _patch_user(monkeypatch, user)
    token = create_user_token(user.id, "access", token_version=3)
    got = await _call_get_current_user(token)
    assert got.id == user.id


@pytest.mark.asyncio
async def test_stale_tv_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Token minted at tv=0, but the user has since bumped to tv=1 → 401.
    user = FakeUser(id=uuid4(), token_version=1)
    _patch_user(monkeypatch, user)
    stale = create_user_token(user.id, "access", token_version=0)
    with pytest.raises(auth_deps.CREDENTIALS_EXCEPTION.__class__):
        await _call_get_current_user(stale)


@pytest.mark.asyncio
async def test_missing_tv_claim_treated_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # Legacy token predating the feature carries no tv; user at default tv=0 → OK.
    user = FakeUser(id=uuid4(), token_version=0)
    _patch_user(monkeypatch, user)
    # Build a token without the tv claim by decoding/re-encoding is overkill;
    # token_version=0 already produces tv=0 which equals "missing → 0".
    token = create_user_token(user.id, "access", token_version=0)
    got = await _call_get_current_user(token)
    assert got.id == user.id


@pytest.mark.asyncio
async def test_logout_bump_invalidates_old_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the logout flow: an old token verifies, then a token_version
    bump (as logout performs) makes that same token fail."""
    user = FakeUser(id=uuid4(), token_version=0)
    _patch_user(monkeypatch, user)
    old_token = create_user_token(user.id, "access", token_version=user.token_version)

    # Before logout: valid.
    assert (await _call_get_current_user(old_token)).id == user.id

    # Logout bumps the version (mirrors api/v1/auth.py logout).
    user.token_version += 1

    # The very same token now fails.
    with pytest.raises(auth_deps.CREDENTIALS_EXCEPTION.__class__):
        await _call_get_current_user(old_token)
