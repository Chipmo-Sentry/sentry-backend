"""ADR-0029 I3 — edge_config_repo.set_global upserts the ONE global edge config
(stored in app_config key='edge_config') and bumps `version` so agents re-apply.
DB-free: a fake session stands in for the async PG session (CI has no postgres)."""

from __future__ import annotations

from typing import Any

from sentry_backend.db.models.app_config import AppConfig
from sentry_backend.repository import edge_config_repo


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Minimal async-session stand-in: execute() returns the preset existing row."""

    def __init__(self, existing: AppConfig | None) -> None:
        self._existing = existing
        self.added: list[Any] = []
        self.flushed = False

    async def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._existing)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


def test_parse_row_unset_is_version_1_empty() -> None:
    assert edge_config_repo.parse_row(None) == (1, {})


def test_parse_row_reads_value() -> None:
    row = AppConfig(key="edge_config", value={"version": 7, "overrides": {"frame_skip": 4}})
    assert edge_config_repo.parse_row(row) == (7, {"frame_skip": 4})


async def test_set_global_new_row_starts_at_version_2() -> None:
    sess = _FakeSession(existing=None)
    row = await edge_config_repo.set_global(sess, {"person_conf": 0.5})  # type: ignore[arg-type]
    assert row.key == "edge_config"
    assert row.value == {"version": 2, "overrides": {"person_conf": 0.5}}
    assert sess.added == [row]  # inserted
    assert sess.flushed is True


async def test_set_global_existing_bumps_version() -> None:
    existing = AppConfig(key="edge_config", value={"version": 5, "overrides": {"frame_skip": 4}})
    sess = _FakeSession(existing=existing)
    row = await edge_config_repo.set_global(sess, {"band_red": 80.0})  # type: ignore[arg-type]
    assert row is existing  # updated in place, not re-inserted
    assert row.value == {"version": 6, "overrides": {"band_red": 80.0}}  # replaced, not merged
    assert sess.added == []
