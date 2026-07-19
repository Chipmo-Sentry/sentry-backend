"""Internal endpoints — service-to-service (e.g. sentry-ai → backend)."""

import base64
import hmac
from typing import Annotated, Literal
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.ai_node import AiNode
from sentry_backend.db.models.alert import AlertLevel
from sentry_backend.db.models.event_log import EventSeverity, EventType
from sentry_backend.deps.ai_node_auth import get_current_ai_node
from sentry_backend.deps.db import get_db
from sentry_backend.deps.service import require_service_token
from sentry_backend.repository import ai_node_repo, alert_repo, rag_case_repo
from sentry_backend.schemas.alert import (
    AlertCreateInternal,
    AlertPublic,
    BreachClearedCreate,
    LiveAlertCreate,
)
from sentry_backend.schemas.rag import RagCaseCreate, RagCaseMatch, RagSimilarRequest
from sentry_backend.security import decode_user_token
from sentry_backend.services import alert_notify, event_log
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level
from sentry_backend.services.footfall_aggregator import get_footfall_aggregator
from sentry_backend.services.live_broker import get_live_broker
from sentry_backend.services.threshold_handler import (
    create_live_alert,
    get_threshold_handler,
    record_breach_cleared,
)
from sentry_backend.settings import get_settings

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

# Map an alert's severity band → event-log severity for the activity timeline.
_ALERT_SEVERITY = {
    AlertLevel.ignore: EventSeverity.info,
    AlertLevel.log: EventSeverity.info,
    AlertLevel.notify: EventSeverity.warning,
    AlertLevel.review: EventSeverity.critical,
}

_INTERNAL_TOKEN_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or revoked internal credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


class MediaMtxAuthRequest(BaseModel):
    """MediaMTX authHTTP payload (subset we use). MediaMTX POSTs this on every
    publish/read/playback/api action when `authMethod: http`."""

    model_config = ConfigDict(extra="ignore")

    user: str = ""
    password: str = ""
    action: str = ""  # publish | read | playback | api | metrics | pprof
    path: str = ""
    query: str = ""  # raw URL query string (carries ?jwt=… for reads)


def _stream_token_allows(query: str, path: str) -> bool:
    """True iff the query carries a valid stream token scoped to `path`."""
    params = parse_qs(query or "")
    token = (params.get("jwt") or params.get("token") or [""])[0]
    if not token:
        return False
    try:
        payload = decode_user_token(token)
    except ValueError:
        return False
    return payload.get("typ") == "stream" and payload.get("path") == path


def _creds_match(user: str, password: str, exp_user: str | None, exp_pass: str | None) -> bool:
    if not exp_user or not exp_pass:
        return False
    return hmac.compare_digest(user, exp_user) and hmac.compare_digest(password, exp_pass)


@router.post("/mediamtx-auth", status_code=status.HTTP_204_NO_CONTENT)
async def mediamtx_auth(
    body: MediaMtxAuthRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """MediaMTX authHTTP authorizer (replaces MediaMTX's open `user: any` read).

    - read/playback → require a valid per-camera stream token in the query
    - publish       → MediaMTX publish creds
    - api/metrics/pprof → MediaMTX control-API creds
    Returns 204 to allow, 401 to deny. Optionally gated by a shared secret so
    only the MediaMTX host can call it.
    """
    settings = get_settings()
    secret = settings.mediamtx_auth_secret
    if secret and authorization != f"Bearer {secret}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad auth secret")

    action = body.action.lower()
    allowed = False
    if action in ("read", "playback"):
        allowed = _stream_token_allows(body.query, body.path)
    elif action == "publish":
        allowed = _creds_match(
            body.user, body.password, settings.mediamtx_publish_user, settings.mediamtx_publish_pass
        )
    elif action in ("api", "metrics", "pprof"):
        allowed = _creds_match(
            body.user, body.password, settings.mediamtx_api_user, settings.mediamtx_api_pass
        )

    if not allowed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="denied")


@router.post("/alerts", response_model=AlertPublic, status_code=status.HTTP_201_CREATED)
async def create_alert_from_ai(
    body: AlertCreateInternal,
    db: Annotated[AsyncSession, Depends(get_db)],
    service_name: Annotated[str, Depends(require_service_token)],
) -> AlertPublic:
    """sentry-ai POSTs here after VLM verification finishes.

    We derive store/camera/org from the clip — sentry-ai doesn't need to
    know the tenancy hierarchy.
    """
    if service_name != "sentry-ai":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Service '{service_name}' not authorized for this endpoint",
        )

    # Look up clip without org scope (service-level access)
    from sqlalchemy import select

    from sentry_backend.db.models.clip import Clip

    result = await db.execute(select(Clip).where(Clip.id == body.clip_id))
    clip = result.scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    alert_level = derive_alert_level(body.category, body.confidence)

    alert = await alert_repo.create_alert(
        db,
        clip_id=body.clip_id,
        organization_id=clip.organization_id,
        store_id=clip.store_id,
        camera_id=clip.camera_id,
        category=body.category,
        confidence=body.confidence,
        reasoning=body.reasoning,
        model_name=body.model_name,
        alert_level=alert_level,
        inference_latency_ms=body.inference_latency_ms,
    )

    # Commit must happen BEFORE publishing — otherwise SSE clients receive
    # an alert ID that briefly doesn't exist in the DB.
    await db.commit()

    payload = AlertPublic.model_validate(alert).model_dump(mode="json")
    await get_broker().publish(clip.organization_id, payload)
    await alert_notify.notify_alert(db, alert)

    # Activity timeline: suspicious activity is one of the headline event types.
    await event_log.emit(
        db,
        event_type=EventType.alert_created,
        severity=_ALERT_SEVERITY.get(alert.alert_level, EventSeverity.warning),
        message=f"Сэжигтэй үйлдэл илрлээ: {alert.category.value} ({alert.confidence:.0%})",
        organization_id=alert.organization_id,
        store_id=alert.store_id,
        camera_id=alert.camera_id,
        actor_label="AI",
        detail={
            "alert_id": str(alert.id),
            "category": alert.category.value,
            "confidence": alert.confidence,
            "alert_level": alert.alert_level.value,
            "model_name": alert.model_name,
        },
    )

    return AlertPublic.model_validate(alert)


@router.post("/live-alert", response_model=AlertPublic, status_code=status.HTTP_201_CREATED)
async def create_live_alert_from_node(
    body: LiveAlertCreate,
    node: Annotated[AiNode, Depends(get_current_ai_node)],
) -> AlertPublic:
    """Node-push live alert. The AI node detected the breach, cut + VLM-verified
    the clip locally, and POSTs the finished result here (outbound — reliable,
    unlike the old cloud→node /v1/cut-verify pull). camera_id = mediamtx_path.

    Auth: the paired AI node's own JWT (typ=ai_node) — the SAME token it uses for
    heartbeat/config. The node never holds a minted service token (pairing sets
    SENTRY_BACKEND_SERVICE_TOKEN to the ai_node JWT), so requiring one here 401'd
    every node-push alert. Authenticating as the node also binds the post to a
    known, active node row."""
    del node  # identity asserted by the dependency; tenancy derived from camera
    try:
        clip_bytes = base64.b64decode(body.clip_b64)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="clip_b64 буруу байна."
        ) from e

    detail = (
        [d.model_dump() for d in body.triggered_behavior_detail]
        if body.triggered_behavior_detail
        else None
    )
    alert = await create_live_alert(
        mediamtx_path=body.camera_id,
        clip_bytes=clip_bytes,
        category=body.category,
        actions=[a.value for a in body.actions] if body.actions else None,
        confidence=body.confidence,
        reasoning=body.reasoning,
        model_name=body.model_name,
        inference_latency_ms=body.inference_latency_ms,
        duration_sec_clip=body.duration_sec_clip,
        captured_at=body.captured_at,
        file_size_bytes=body.file_size_bytes,
        embedding=body.embedding,
        person_id=body.person_id,
        peak_risk_pct=body.peak_risk_pct,
        behaviors=body.triggered_behaviors,
        sequences=body.triggered_sequences,
        behavior_detail=detail,
        pose_sequence=body.pose_sequence,
    )
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Камер олдсонгүй эсвэл клип хадгалж чадсангүй.",
        )
    return alert


@router.post("/breach-cleared", status_code=status.HTTP_204_NO_CONTENT)
async def report_breach_cleared(
    body: BreachClearedCreate,
    node: Annotated[AiNode, Depends(get_current_ai_node)],
) -> None:
    """Observability for breaches the node dropped without alerting (VLM cleared
    / clip-cut failed). Writes a risk_episode row so the miss is VISIBLE on the
    activity timeline instead of silently lost. camera_id = mediamtx_path.

    Auth: the paired AI node's own JWT (typ=ai_node) — same as /live-alert."""
    del node
    await record_breach_cleared(
        mediamtx_path=body.camera_id,
        reason=body.reason,
        peak_risk_pct=body.peak_risk_pct,
        person_id=body.person_id,
        category=body.category.value if body.category else None,
        confidence=body.confidence,
        behaviors=body.triggered_behaviors,
    )


# ===== M1-LIVE L3: live metadata fanout =====


class LiveTrack(BaseModel):
    """One detected person in one frame (REV.1 — mirrors sentry-ai's
    TrackPayload). Lenient: unknown keys ignored, display fields defaulted so a
    schema addition on the AI side never 422s the whole batch."""

    model_config = ConfigDict(extra="ignore")

    person_id: int
    box: tuple[float, float, float, float]
    det_confidence: float = 0.0
    # Accumulated risk score (absolute, not percent — see /api/v1/behaviors).
    risk_pct: float = Field(default=0.0, ge=0.0)
    color: Literal["green", "yellow", "red"] = "green"
    # v2 behavior engine context (REV.2). These previously existed only on the
    # AI side and were silently stripped here (extra="ignore") — so sequences /
    # store_person_id never reached browsers or the threshold handler.
    level: str = "LOW"
    state: str = "IDLE"
    sequences: list[str] = Field(default_factory=list)
    # Episode-accumulated criteria: stable keys (first-fired order), per-key
    # banked score, Mongolian display strings, and when the episode opened.
    behaviors: list[str] = Field(default_factory=list)
    behavior_scores: dict[str, float] = Field(default_factory=dict)
    # Seconds from episode start to each criterion's first firing. Carried here
    # (not just for the overlay) so the threshold handler can bank it onto the
    # alert for the detail-page timeline — extra="ignore" would strip it.
    behavior_offsets: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    episode_started_ms: int | None = None
    # Cross-camera re-ID (ADR-0023)
    store_person_id: int | None = None
    store_risk_pct: float | None = None
    # F5 demographics (docs/30) — optional classifier attributes. Lenient str
    # (not Literal) so a node-side vocabulary tweak never 422s a whole batch;
    # the footfall aggregator normalizes into its closed buckets. extra="ignore"
    # strips undeclared keys, so they must be declared to reach the aggregator.
    gender: str | None = Field(default=None, max_length=16)
    age_band: str | None = Field(default=None, max_length=16)
    # Staff identification (badge-color + VLM lock on the node). Declared or
    # extra="ignore" strips it: the browser needs it to recolor the box, the
    # aggregator to exclude staff from visitor KPIs/paths/heatmap. Alerts are
    # NOT filtered on it — internal theft stays monitored.
    is_staff: bool = False


class LiveItem(BaseModel):
    """A detected item for the live overlay (mirrors sentry-ai's ItemPayload).

    extra="ignore" would strip these on the parse+re-dump in receive_live_metadata,
    so they must be declared here to reach the browser's held-item box + name."""

    model_config = ConfigDict(extra="ignore")

    box: tuple[float, float, float, float]
    label: str = ""
    label_mn: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    held: bool = False


class LiveFrame(BaseModel):
    """Per-analyzed-frame metadata from the sentry-ai live worker
    (REV.1 — mirrors sentry-ai's FrameMetadata)."""

    model_config = ConfigDict(extra="ignore")

    camera_id: str = Field(min_length=1, max_length=128)
    frame_id: int = 0
    ts_ms: int = 0
    width: int = 0
    height: int = 0
    fps_inference: float = 0.0
    tracks: list[LiveTrack] = Field(default_factory=list)
    # Detected merchandise this frame (held-item box + Mongolian name overlay).
    items: list[LiveItem] = Field(default_factory=list)


class LiveMetadataBatch(BaseModel):
    """Batch of per-frame metadata from sentry-ai live worker."""

    frames: list[LiveFrame] = Field(min_length=1, max_length=200)


async def _require_simple_internal_token(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Simpler shared-secret auth for high-volume live metadata.

    sentry-ai sends `Authorization: Bearer <token>`. We accept any of:
      - A bearer token matching `live_metadata_shared_secret` (env), OR
      - A paired node's ai_node JWT (typ=ai_node) — signature + exp AND a single
        node lookup to confirm the node is still active (revoke-aware), OR
      - A valid full service JWT (delegates to require_service_token).
    """
    settings = get_settings()
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(maxsplit=1)[1]
        expected = settings.live_metadata_shared_secret
        if expected and hmac.compare_digest(token, expected):
            return
        # Paired AI node token: signature-validated, then a single node lookup
        # so a revoked (is_active=False) node's still-valid-by-exp JWT is
        # rejected. One indexed PK query per batch is acceptable on this path
        # (batches carry up to 200 frames, so the lookup amortizes well).
        try:
            payload = decode_user_token(token)
        except ValueError:
            payload = None
        if payload is not None and payload.get("typ") == "ai_node":
            try:
                node_id = UUID(payload["sub"])
            except (KeyError, ValueError):
                raise _INTERNAL_TOKEN_EXCEPTION from None
            node = await ai_node_repo.get_node(db, node_id)
            if node is None or not node.is_active:
                raise _INTERNAL_TOKEN_EXCEPTION
            return
    # Fall back to JWT path
    await require_service_token(authorization)


@router.post("/live-metadata", status_code=status.HTTP_202_ACCEPTED)
async def receive_live_metadata(
    body: LiveMetadataBatch,
    _: Annotated[None, Depends(_require_simple_internal_token)],
) -> dict[str, int]:
    """sentry-ai live worker POSTs batched per-frame metadata here.

    Each frame is (a) dispatched to subscribers of its `camera_id` (browser
    WebSocket clients on `/ws/live/{camera_id}`) and (b) fed through the
    threshold handler to fire alerts on sustained risk_pct breaches (L5).
    """
    broker = get_live_broker()
    threshold_handler = get_threshold_handler()
    footfall = get_footfall_aggregator()
    published = 0
    for frame in body.frames:
        # Downstream (WS fanout + threshold handler) consume the raw dict shape,
        # so dump back to JSON-mode dict — the wire payload to browsers is
        # unchanged, but the batch is now validated/typed on the way in.
        frame_dict = frame.model_dump(mode="json")
        await broker.publish(frame.camera_id, frame_dict)
        await threshold_handler.on_frame(frame_dict)
        # docs/30 F2: accumulate footfall for the /insights heatmap. Buffered +
        # batch-flushed inside the aggregator, so this stays cheap on the hot path.
        await footfall.on_frame(frame_dict)
        published += 1
    return {"received": len(body.frames), "published": published}


# ── RAG verified-case store (docs/19 Phase 4) ───────────────────────────────
@router.post("/rag/cases", status_code=status.HTTP_201_CREATED)
async def rag_add_case(
    body: RagCaseCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(_require_simple_internal_token)],
) -> dict[str, str]:
    """Store a staff-verified event (embedded by the AI node) for future RAG
    retrieval. Called when feedback confirms/denies a suspicion."""
    case = await rag_case_repo.add_case(
        db,
        store_id=body.store_id,
        verdict=body.verdict,
        category=body.category,
        description=body.description,
        embedding=body.embedding,
    )
    await db.commit()
    return {"id": str(case.id)}


@router.post("/rag/similar", response_model=list[RagCaseMatch])
async def rag_similar(
    body: RagSimilarRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(_require_simple_internal_token)],
) -> list[RagCaseMatch]:
    """Top-k past verified cases most similar to the query embedding — the AI
    node injects these into the VLM verify prompt as few-shot examples."""
    matches = await rag_case_repo.similar_cases(
        db, store_id=body.store_id, embedding=body.embedding, k=body.k
    )
    return [
        RagCaseMatch(
            description=c.description,
            category=c.category,
            verdict=c.verdict,
            score=round(score, 4),
        )
        for c, score in matches
        if score > 0
    ]
