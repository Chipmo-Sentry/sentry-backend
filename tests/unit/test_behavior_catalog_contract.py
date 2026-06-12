"""Behavior catalog drift-guard (T17 #2).

The behavior dimension/sequence keys are duplicated as string literals across
three repos and synced BY HAND:

  * sentry-backend src/sentry_backend/api/v1/behaviors.py  (BUILTIN_META,
                   SEQUENCE_META — this repo)
  * sentry-ai      src/sentry_ai/live_worker/behavior.py   (DEFAULT_WEIGHTS,
                   DEFAULT_SEQUENCES)
  * sentry-superadmin BehaviorsPage (display only)

KEEP IN SYNC: sentry-ai has the MIRROR of this file at
tests/unit/test_behavior_catalog_contract.py with the IDENTICAL literal lists
below. If you add/remove/rename a key, update BOTH test files plus
sentry-ai DEFAULT_WEIGHTS/DEFAULT_SEQUENCES and the superadmin BehaviorsPage.
"""

from __future__ import annotations

from sentry_backend.api.v1.behaviors import BUILTIN_META, SEQUENCE_META

# Dimension keys with a live detector in sentry-ai (DEFAULT_WEIGHTS).
# Mirrored in the sentry-ai contract test as AI_DIMENSION_KEYS.
AI_DIMENSION_KEYS: list[str] = [
    "bag_interaction",
    "body_block",
    "crouch",
    "item_pickup",
    "loitering",
    "looking_around",
    "pocket_interaction",
    "rapid_movement",
    "wrist_to_torso",
]

# Sequence rule keys (sentry-ai DEFAULT_SEQUENCES / backend SEQUENCE_META).
# Mirrored in the sentry-ai contract test as SEQUENCE_KEYS.
SEQUENCE_KEYS: list[str] = [
    "concealment_sequence",
    "seq_loiter_pickup_conceal",
    "seq_look_pickup",
    "seq_pickup_bag",
    "seq_pickup_wrist",
    "seq_pickup_wrist_bag",
]

# Full BUILTIN_META catalog (15 = 9 live detectors + concealment_sequence +
# 5 inert placeholders awaiting detectors). Backend-only — sentry-ai does not
# know the inert keys.
CATALOG_KEYS: list[str] = [
    "bag_interaction",
    "body_block",
    "concealment_sequence",
    "coordinated_activity",
    "crouch",
    "exit_after_concealment",
    "group_distraction",
    "item_pickup",
    "loitering",
    "looking_around",
    "pocket_interaction",
    "rapid_movement",
    "repeated_shelf_visit",
    "rfid_mismatch",
    "wrist_to_torso",
]

_SYNC_MSG = (
    "Behavior catalog key set changed! Update sentry-ai DEFAULT_WEIGHTS/"
    "DEFAULT_SEQUENCES (live_worker/behavior.py), the superadmin BehaviorsPage,"
    " AND the mirrored lists in BOTH test_behavior_catalog_contract.py files"
    " (sentry-backend + sentry-ai) together."
)


def test_builtin_meta_keys_match_contract() -> None:
    assert sorted(m["key"] for m in BUILTIN_META) == CATALOG_KEYS, _SYNC_MSG


def test_sequence_meta_keys_match_contract() -> None:
    assert sorted(m["key"] for m in SEQUENCE_META) == SEQUENCE_KEYS, _SYNC_MSG


def test_detector_backed_keys_match_sentry_ai() -> None:
    # has_detector=True ⇔ sentry-ai scores it live: the 9 weighted dimensions
    # plus the concealment_sequence critical sequence rule.
    live = sorted(m["key"] for m in BUILTIN_META if m["has_detector"])
    assert live == sorted([*AI_DIMENSION_KEYS, "concealment_sequence"]), _SYNC_MSG
