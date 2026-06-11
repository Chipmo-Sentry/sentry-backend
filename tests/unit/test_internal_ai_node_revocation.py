"""AI-node revocation on the high-volume internal path (T01 / H2):

``_require_simple_internal_token`` accepts a paired node's ai_node JWT, but a
node that has been revoked (``is_active=False``) must be rejected even though
its long-lived token is still valid by signature + exp.

Drives the dependency directly with a stub session + monkeypatched
``ai_node_repo.get_node`` (no real DB).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from sentry_backend.api.v1 import internal
from sentry_backend.security import create_ai_node_token


@dataclass
class FakeNode:
    id: UUID
    is_active: bool = True


class FakeSession:
    pass


def _patch_node(monkeypatch: pytest.MonkeyPatch, node: FakeNode | None) -> None:
    async def fake_get(_db: object, _nid: UUID) -> FakeNode | None:
        return node

    monkeypatch.setattr(internal.ai_node_repo, "get_node", fake_get)


async def _call(token: str) -> None:
    await internal._require_simple_internal_token(
        db=FakeSession(),  # type: ignore[arg-type]
        authorization=f"Bearer {token}",
    )


async def test_active_node_token_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    node = FakeNode(id=uuid4(), is_active=True)
    _patch_node(monkeypatch, node)
    # No exception raised → accepted.
    await _call(create_ai_node_token(node.id))


async def test_revoked_node_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    node = FakeNode(id=uuid4(), is_active=False)
    _patch_node(monkeypatch, node)
    with pytest.raises(internal._INTERNAL_TOKEN_EXCEPTION.__class__):
        await _call(create_ai_node_token(node.id))


async def test_unknown_node_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Token references a node id that no longer exists in the DB.
    _patch_node(monkeypatch, None)
    with pytest.raises(internal._INTERNAL_TOKEN_EXCEPTION.__class__):
        await _call(create_ai_node_token(uuid4()))


async def test_shared_secret_still_works_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env shared-secret path must short-circuit before any node lookup."""
    from sentry_backend.settings import get_settings

    settings = get_settings()
    secret = "live-metadata-shared-secret-xyz"
    monkeypatch.setattr(settings, "live_metadata_shared_secret", secret)

    # get_node must NOT be called — make it explode if it is.
    async def boom(_db: object, _nid: UUID) -> None:
        raise AssertionError("node lookup should be skipped for shared secret")

    monkeypatch.setattr(internal.ai_node_repo, "get_node", boom)

    await internal._require_simple_internal_token(
        db=FakeSession(),  # type: ignore[arg-type]
        authorization=f"Bearer {secret}",
    )
