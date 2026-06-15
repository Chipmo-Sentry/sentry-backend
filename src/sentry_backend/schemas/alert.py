"""Alert schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sentry_backend.db.models.alert import AlertCategory, AlertLevel, AlertTrigger
from sentry_backend.db.models.feedback import FeedbackVerdict


class BehaviorDetailItem(BaseModel):
    """One row of the alert's behaviour timeline (first-fired order)."""

    key: str
    offset_sec: float  # seconds from the episode's first firing
    score: float  # points this criterion banked this episode


class AlertPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    clip_id: UUID
    organization_id: UUID
    store_id: UUID | None
    camera_id: UUID | None
    category: AlertCategory
    confidence: float
    reasoning: str
    model_name: str
    alert_level: AlertLevel
    inference_latency_ms: int
    created_at: datetime
    # L5 live-threshold context (null for manual_upload alerts)
    triggered_by: AlertTrigger = AlertTrigger.manual_upload
    person_id: int | None = None
    peak_risk_pct: float | None = None
    # Behavior-engine episode context at breach time: fired criterion keys
    # (first-fired order) + completed sequence rule keys. Labels via /behaviors.
    triggered_behaviors: list[str] | None = None
    triggered_sequences: list[str] | None = None
    # Per-criterion timeline (key, seconds-from-episode-start, banked score) in
    # first-fired order — drives the alert detail timeline. Null when unavailable
    # (manual uploads, older alerts, or a node without offsets).
    triggered_behavior_detail: list[BehaviorDetailItem] | None = None
    # Staff review verdict, if one has been given (true_positive / false_positive
    # / unclear). None = not yet reviewed → the UI shows the verdict buttons; set
    # = the UI shows the settled answer instead of re-asking.
    feedback_verdict: FeedbackVerdict | None = None


class AlertCreateInternal(BaseModel):
    """POST /api/v1/internal/alerts — sentry-ai service posts this."""

    clip_id: UUID
    category: AlertCategory
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=2000)
    model_name: str = Field(min_length=1, max_length=64)
    inference_latency_ms: int = Field(ge=0)


class LiveAlertCreate(BaseModel):
    """POST /api/v1/internal/live-alert — node-push path (ADR live-alert).

    The AI node detected a sustained risk breach, cut the clip + ran VLM
    LOCALLY, and posts the finished alert here (outbound, reliable). camera_id
    is the Camera.mediamtx_path; the backend resolves org/store. The node only
    posts non-ignore verdicts (it applies the VLM gate itself)."""

    camera_id: str = Field(min_length=1)  # Camera.mediamtx_path
    category: AlertCategory
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=2000)
    model_name: str = Field(min_length=1, max_length=64)
    inference_latency_ms: int = Field(ge=0)
    # Clip (base64-encoded mp4 the node cut from its local recordings).
    clip_b64: str = Field(min_length=1)
    file_size_bytes: int = Field(ge=0)
    duration_sec_clip: float = Field(ge=0.0)
    captured_at: datetime
    embedding: list[float] | None = None
    # Live episode context.
    person_id: int | None = None
    peak_risk_pct: float | None = None
    triggered_behaviors: list[str] | None = None
    triggered_sequences: list[str] | None = None
    triggered_behavior_detail: list[BehaviorDetailItem] | None = None
