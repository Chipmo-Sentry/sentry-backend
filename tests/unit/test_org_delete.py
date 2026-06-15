"""Org deletion (T15 #3 — the privacy policy's "delete your account" promise).

Covers the shared service (clip files unlinked from disk, org row deleted) and
both endpoints' guards: owner-only + slug confirmation for self-service, 404
for super-admin. Style matches the suite: stub session + monkeypatched repos,
no real DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sentry_backend.api.v1 import admin as admin_api
from sentry_backend.api.v1 import org_team as org_api
from sentry_backend.db.models.organization import OrgRole
from sentry_backend.schemas.org_team import OrgDeleteConfirm
from sentry_backend.services import org_delete
from sentry_backend.services.org_delete import ClipFileSweep, remove_clip_files


@dataclass
class FakeOrg:
    id: UUID = field(default_factory=uuid4)
    slug: str = "naran-market"
    name: str = "Наран маркет"


@dataclass
class FakeUser:
    id: UUID = field(default_factory=uuid4)
    is_super_admin: bool = False
    is_active: bool = True
    email: str = "owner@example.com"


class FakeScalars:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def all(self) -> list[str]:
        return self._values


class FakeResult:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def scalars(self) -> FakeScalars:
        return FakeScalars(self._values)


class FakeSession:
    """Stub AsyncSession: first execute() is the storage_path SELECT, the
    second is the org DELETE (recorded so tests assert it was issued)."""

    def __init__(self, storage_paths: list[str]) -> None:
        self._storage_paths = storage_paths
        self.statements: list[Any] = []
        self.flushed = False

    async def execute(self, stmt: Any) -> FakeResult:
        self.statements.append(stmt)
        return FakeResult(self._storage_paths)

    async def flush(self) -> None:
        self.flushed = True


# --- remove_clip_files (the part a DB cascade can never do) -----------------


def test_existing_files_are_unlinked(tmp_path: Path) -> None:
    files = [tmp_path / f"clip{i}.mp4" for i in range(3)]
    for f in files:
        f.write_bytes(b"evidence")
    sweep = remove_clip_files([str(f) for f in files])
    assert sweep == ClipFileSweep(removed=3, missing=0, failed=0)
    assert not any(f.exists() for f in files)


def test_missing_files_are_skipped(tmp_path: Path) -> None:
    present = tmp_path / "here.mp4"
    present.write_bytes(b"x")
    sweep = remove_clip_files([str(present), str(tmp_path / "gone.mp4")])
    assert sweep == ClipFileSweep(removed=1, missing=1, failed=0)


def test_unlink_error_is_counted_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "locked.mp4"
    f.write_bytes(b"x")

    def _boom(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", _boom)
    sweep = remove_clip_files([str(f)])
    assert sweep == ClipFileSweep(removed=0, missing=0, failed=1)


# --- delete_organization (files first, then the row) -------------------------


@pytest.mark.asyncio
async def test_delete_organization_removes_files_then_row(tmp_path: Path) -> None:
    f = tmp_path / "ev.mp4"
    f.write_bytes(b"x")
    db = FakeSession(storage_paths=[str(f)])
    org = FakeOrg()

    sweep = await org_delete.delete_organization(db, org)  # type: ignore[arg-type]

    assert sweep.removed == 1
    assert not f.exists()
    # SELECT storage_paths + DELETE organizations, then flush.
    assert len(db.statements) == 2
    assert db.flushed


@pytest.mark.asyncio
async def test_delete_organization_proceeds_when_files_already_gone(tmp_path: Path) -> None:
    # Retention (or a lost volume) already removed the file — erasure proceeds.
    db = FakeSession(storage_paths=[str(tmp_path / "gone.mp4")])
    sweep = await org_delete.delete_organization(db, FakeOrg())  # type: ignore[arg-type]
    assert sweep == ClipFileSweep(removed=0, missing=1, failed=0)
    assert db.flushed


# --- owner self-service guards (DELETE /api/v1/org) --------------------------


def _patch_role(monkeypatch: pytest.MonkeyPatch, role: OrgRole | None) -> None:
    async def fake_role(_db: object, *, user_id: UUID, organization_id: UUID) -> OrgRole | None:
        return role

    monkeypatch.setattr(org_api.org_repo, "get_member_role", fake_role)


def _patch_get_org(monkeypatch: pytest.MonkeyPatch, org: FakeOrg | None) -> None:
    async def fake_get(_db: object, _org_id: UUID) -> FakeOrg | None:
        return org

    monkeypatch.setattr(org_api.org_repo, "get_org", fake_get)


async def _call_delete_my_org(confirm_slug: str) -> object:
    return await org_api.delete_my_org(
        body=OrgDeleteConfirm(confirm_slug=confirm_slug),
        db=FakeSession([]),  # type: ignore[arg-type]
        org_id=uuid4(),
        actor=FakeUser(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [OrgRole.admin, OrgRole.staff, None])
async def test_non_owner_gets_403(monkeypatch: pytest.MonkeyPatch, role: OrgRole | None) -> None:
    _patch_role(monkeypatch, role)
    with pytest.raises(HTTPException) as exc:
        await _call_delete_my_org("naran-market")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_wrong_confirm_slug_gets_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_role(monkeypatch, OrgRole.owner)
    _patch_get_org(monkeypatch, FakeOrg(slug="naran-market"))
    with pytest.raises(HTTPException) as exc:
        await _call_delete_my_org("wrong-slug")
    assert exc.value.status_code == 400
    assert "slug" in exc.value.detail


@pytest.mark.asyncio
async def test_owner_with_correct_slug_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_role(monkeypatch, OrgRole.owner)
    org = FakeOrg(slug="naran-market")
    _patch_get_org(monkeypatch, org)
    deleted: list[FakeOrg] = []

    async def fake_delete(_db: object, target: FakeOrg) -> ClipFileSweep:
        deleted.append(target)
        return ClipFileSweep(removed=2)

    monkeypatch.setattr(org_api.org_delete, "delete_organization", fake_delete)
    response = await _call_delete_my_org("naran-market")
    assert getattr(response, "status_code", None) == 204
    assert deleted == [org]


# --- super-admin endpoint (DELETE /api/v1/admin/orgs/{id}) --------------------


@pytest.mark.asyncio
async def test_admin_delete_unknown_org_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(_db: object, _org_id: UUID) -> None:
        return None

    monkeypatch.setattr(admin_api.org_repo, "get_org", fake_get)
    with pytest.raises(HTTPException) as exc:
        await admin_api.delete_org(
            org_id=uuid4(),
            db=FakeSession([]),  # type: ignore[arg-type]
            _=FakeUser(is_super_admin=True),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_delete_removes_org_and_clip_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end through the real service: clip file gone + org DELETE issued."""
    f = tmp_path / "evidence.mp4"
    f.write_bytes(b"x")
    org = FakeOrg()

    async def fake_get(_db: object, _org_id: UUID) -> FakeOrg:
        return org

    monkeypatch.setattr(admin_api.org_repo, "get_org", fake_get)
    db = FakeSession(storage_paths=[str(f)])
    await admin_api.delete_org(
        org_id=org.id,
        db=db,  # type: ignore[arg-type]
        _=FakeUser(is_super_admin=True),  # type: ignore[arg-type]
    )
    assert not f.exists()
    assert db.flushed
