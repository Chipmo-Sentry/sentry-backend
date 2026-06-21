"""ADR-0029 I3 — edge_config_repo.set_overrides upserts a store's overrides and
bumps `version` so agents re-apply. DB-free: a fake session stands in for the
async PG session (CI has no postgres)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sentry_backend.db.models.edge_config import StoreEdgeConfig
from sentry_backend.repository import edge_config_repo


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Minimal async-session stand-in: execute() returns the preset existing row."""

    def __init__(self, existing: StoreEdgeConfig | None) -> None:
        self._existing = existing
        self.added: list[Any] = []
        self.flushed = False

    async def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._existing)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


async def test_set_overrides_new_row_starts_at_version_2() -> None:
    sess = _FakeSession(existing=None)
    sid = uuid4()
    row = await edge_config_repo.set_overrides(sess, sid, {"person_conf": 0.5})  # type: ignore[arg-type]
    assert row.version == 2  # detectable change from the version-1 defaults
    assert row.overrides == {"person_conf": 0.5}
    assert sess.added == [row]  # inserted
    assert sess.flushed is True


async def test_set_overrides_existing_bumps_version() -> None:
    existing = StoreEdgeConfig(store_id=uuid4(), version=5, overrides={"frame_skip": 4})
    sess = _FakeSession(existing=existing)
    row = await edge_config_repo.set_overrides(sess, existing.store_id, {"band_red": 80.0})  # type: ignore[arg-type]
    assert row is existing  # updated in place, not re-inserted
    assert row.version == 6
    assert row.overrides == {"band_red": 80.0}  # replaced, not merged
    assert sess.added == []
