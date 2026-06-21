"""ADR-0029 — GET /agent/edge-config contract. EdgeConfigPayload field names +
defaults MUST stay in sync with sentry_agent_pc.edge.config.EdgeConfig (the agent
keeps its own defaults for any field the backend omits, so drift is silent)."""

from __future__ import annotations

from sentry_backend.schemas.edge import EdgeConfigPayload

# The 24 tunables the agent's EdgeConfig defines (besides `version`).
_AGENT_FIELDS = {
    "person_conf",
    "item_conf",
    "frame_skip",
    "w_holding",
    "w_conceal",
    "w_wrist_torso",
    "reach_frac",
    "near_frac",
    "min_kp_conf",
    "decay",
    "open_risk",
    "close_risk",
    "post_quiet_sec",
    "drop_after_sec",
    "iou_match",
    "band_yellow",
    "band_red",
    "pre_sec",
    "post_sec",
    "segment_sec",
    "keep_sec",
    "max_clips",
    "max_age_sec",
    "upload_clips",
}


def test_payload_carries_all_24_fields_plus_version() -> None:
    dumped = EdgeConfigPayload().model_dump()
    assert "version" in dumped
    assert set(dumped) >= _AGENT_FIELDS, _AGENT_FIELDS - set(dumped)
    assert len(dumped) == len(_AGENT_FIELDS) + 1  # 24 tunables + version


def test_defaults_match_agent_edge_config() -> None:
    c = EdgeConfigPayload()
    assert c.version == 1
    assert c.person_conf == 0.35
    assert c.item_conf == 0.40
    assert c.frame_skip == 3
    assert c.w_conceal == 14.0
    assert c.min_kp_conf == 0.30
    assert c.iou_match == 0.3
    assert c.pre_sec == 3.0 and c.post_sec == 3.0
    assert c.segment_sec == 1.0
    assert c.max_clips == 50
    assert c.max_age_sec == float(7 * 24 * 3600)
    assert c.upload_clips is True


def test_partial_override_keeps_other_defaults() -> None:
    c = EdgeConfigPayload(person_conf=0.5, version=7)
    assert c.person_conf == 0.5
    assert c.version == 7
    assert c.item_conf == 0.40  # untouched
