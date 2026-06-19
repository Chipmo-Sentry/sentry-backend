"""In-memory latest-diag store, keyed by ai_node id.

The node pushes a rich per-stage diagnostics snapshot (worker errors, VLM
verdicts + raw-on-failure) every ~20s; we keep only the latest per node so the
customer stage-detail pages can read "what is wrong right now". Single-replica
(ADR-0025) — in-process memory, same as the live/alert brokers.
"""

from __future__ import annotations

import time
from uuid import UUID

# node_id -> (received_at_epoch, diag_json)
_latest: dict[UUID, tuple[float, dict[str, object]]] = {}


def put(node_id: UUID, diag: dict[str, object]) -> None:
    _latest[node_id] = (time.time(), diag)


def get(node_id: UUID) -> tuple[float, dict[str, object]] | None:
    """Returns (received_at_epoch, diag) or None if this node never pushed."""
    return _latest.get(node_id)
