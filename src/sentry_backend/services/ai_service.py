"""Background trigger: send a freshly-uploaded clip to sentry-ai, store
the returned alert, and push to SSE subscribers.

M1 flow (laptop, single-process):
    POST /api/v1/clips → creates Clip → asyncio.create_task(verify_clip_with_ai)
    │                                                       │
    │ returns ClipPublic immediately                        │ background
    │                                                       ▼
    │                                              httpx POST sentry-ai /v1/verify
    │                                                       │ waits ~5-15s
    │                                                       ▼
    │                                              derive_alert_level + insert Alert
    │                                                       │
    │                                                       ▼
    │                                              broker.publish → SSE clients

M2+ migration: sentry-ai posts to /api/v1/internal/alerts instead (already
exists, uses service-token auth), so backend doesn't have to wait for VLM.
For M1 the simpler sync-from-backend model is fine."""

import asyncio
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select

from sentry_backend.db.models.alert import AlertCategory
from sentry_backend.db.models.clip import Clip
from sentry_backend.db.session import session_scope
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import alert_repo
from sentry_backend.schemas.alert import AlertPublic
from sentry_backend.services import alert_notify
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.ai_service")


# === RAG query builders (docs/19 Phase 4, T08) ===
#
# verified_case rows are created from staff feedback on alerts, and their
# `description` + embedding come from the VLM's `reasoning` field — short
# MONGOLIAN sentences describing a suspected concealment event (see
# prompts/verify_v1.j2 in sentry-ai and feedback.py here). For cosine
# similarity over those embeddings to rank meaningfully, the retrieval query
# must be the same kind of text: short Mongolian sentences describing what we
# suspect is happening — NOT raw rule keys or English labels.

# Behavior-engine sequence rule keys (sentry-ai live_worker/behavior.py) →
# Mongolian phrases in the same register as the VLM reasoning sentences.
_SEQUENCE_MN: dict[str, str] = {
    "item_pickup": "бараа авсан",
    "wrist_to_torso": "бугуйгаа цээж, бие рүүгээ ойртуулсан",
    "bag_interaction": "цүнх, уут руу гар хийсэн",
    "pocket_interaction": "халаас орчимд гараа хүргэсэн",
    "loitering": "тавиурын дэргэд удаан эргэлдсэн",
    "concealment": "бараа нуун далдалсан",
    "seq_pickup_bag": "бараа аваад цүнхэнд хийсэн",
    "seq_pickup_wrist_bag": "бараа аваад бие рүүгээ ойртуулж цүнхэнд нуусан",
    "seq_loiter_pickup_conceal": "удаан эргэлдэж байгаад бараа авч нуусан",
    "concealment_sequence": "бараа авч халаас, цүнхэндээ нуусан бүрэн дараалал",
}

# sentry-ai VerifyRequest.rag_query has max_length=2000.
_RAG_QUERY_MAX_LEN = 2000


def build_upload_rag_query() -> str:
    """Query for the manual-upload verify path.

    No behavior-engine context exists yet (the clip was just uploaded), so the
    query describes the generic suspected event — concealment in a retail store
    — which surfaces this store's most relevant past staff-verified cases.
    """
    return (
        "Дэлгүүрийн хяналтын бичлэг: хүн бараа авч халаас, цүнх эсвэл "
        "хувцасныхаа доор нуусан байж болзошгүй сэжигтэй үйлдэл."
    )


def build_live_rag_query(peak_risk_pct: float, sequences: list[str] | None = None) -> str:
    """Query for the live-threshold (cut-verify) path.

    Folds the behavior engine's fired sequence rules into Mongolian phrases so
    the query text matches the language of the stored case descriptions.
    """
    parts = [
        f"Амьд хяналт: хүн бараа авч нуусан байж болзошгүй (эрсдэлийн оноо {peak_risk_pct:.0f}%)."
    ]
    phrases = [_SEQUENCE_MN[s] for s in (sequences or []) if s in _SEQUENCE_MN]
    if phrases:
        parts.append("Илэрсэн үйлдлүүд: " + "; ".join(dict.fromkeys(phrases)) + ".")
    return " ".join(parts)[:_RAG_QUERY_MAX_LEN]


async def verify_clip_with_ai(clip_id: UUID) -> None:
    """Background task — never raises; logs failures.

    Detached from the request lifecycle on purpose: the user's upload
    response already came back; if sentry-ai is unreachable we just log
    and leave the clip unverified (a manual /v1/verify retry could re-run
    it later — Session 3 work).
    """
    settings = get_settings()
    if not settings.sentry_ai_url:
        log.warning("sentry_ai_url not configured, skipping verify", clip_id=str(clip_id))
        return

    try:
        await _verify_inner(clip_id, settings.sentry_ai_url, settings.sentry_ai_timeout_sec)
    except Exception as e:  # noqa: BLE001 — background tasks must never raise
        log.exception("verify task crashed", clip_id=str(clip_id), error=str(e))


async def verify_edge_clip(
    clip_path: Path,
    *,
    store_id: str | None = None,
    rag_query: str | None = None,
    provider: str | None = None,
) -> dict[str, object] | None:
    """POST an edge clip's BYTES to sentry-ai ``/v1/edge-clip-upload`` (multipart)
    and return the VLM verdict dict, or None if sentry-ai is unset / unreachable /
    non-200.

    Bytes transport (not a clip_path) because the GPU node and this backend run on
    different hosts (ADR-0029) — sentry-ai cannot read the backend's disk.
    """
    settings = get_settings()
    if not settings.sentry_ai_url:
        log.warning("sentry_ai_url not configured, skipping edge verify")
        return None

    token = settings.sentry_ai_service_token
    headers = {"Authorization": f"Bearer {token}"} if token else None
    form = {
        k: v
        for k, v in {"store_id": store_id, "rag_query": rag_query, "provider": provider}.items()
        if v is not None
    }
    url = f"{settings.sentry_ai_url.rstrip('/')}/v1/edge-clip-upload"
    try:
        clip_bytes = await asyncio.to_thread(clip_path.read_bytes)
        files = {"clip": (clip_path.name, clip_bytes, "video/mp4")}
        async with httpx.AsyncClient(timeout=settings.sentry_ai_timeout_sec) as client:
            resp = await client.post(url, data=form, files=files, headers=headers)
    except (httpx.HTTPError, TimeoutError, OSError) as e:
        log.warning("edge verify call failed", error=str(e))
        return None
    if resp.status_code != 200:
        log.error("sentry-ai edge-clip non-200", status=resp.status_code, body=resp.text[:300])
        return None
    return resp.json()  # type: ignore[no-any-return]


async def _verify_inner(clip_id: UUID, ai_base_url: str, timeout_sec: int) -> None:
    """The actual work, wrapped by the catch-all above."""
    # 1. Load the clip metadata so we have the on-disk path
    clip_payload: dict[str, object] | None = None
    storage_path: str | None = None
    async with session_scope() as db:
        clip = (await db.execute(select(Clip).where(Clip.id == clip_id))).scalar_one_or_none()
        if clip is None:
            log.error("clip not found", clip_id=str(clip_id))
            return
        storage_path = clip.storage_path
        clip_payload = {
            "store_id": str(clip.store_id) if clip.store_id else None,
            "camera_id": str(clip.camera_id) if clip.camera_id else None,
            "organization_id": clip.organization_id,
        }

    # 2. Call sentry-ai /v1/verify (synchronous from our POV, but already
    # out of the user's request path).
    body = {
        "clip_path": storage_path,
        "store_id": clip_payload["store_id"],
        "camera_id": clip_payload["camera_id"],
        # RAG (T08): describe the suspected event so the AI node retrieves this
        # store's similar staff-verified cases as few-shot context for the VLM.
        "rag_query": build_upload_rag_query(),
    }
    log.info("calling sentry-ai", clip_id=str(clip_id), url=f"{ai_base_url}/v1/verify")
    token = get_settings().sentry_ai_service_token
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        resp = await client.post(f"{ai_base_url}/v1/verify", json=body, headers=headers)

    if resp.status_code != 200:
        log.error(
            "sentry-ai non-200", clip_id=str(clip_id), status=resp.status_code, body=resp.text[:500]
        )
        return

    data = resp.json()

    # 3. Insert Alert
    try:
        category = AlertCategory(data["category"])
    except (KeyError, ValueError) as e:
        log.error("invalid category from sentry-ai", clip_id=str(clip_id), data=data, error=str(e))
        return

    alert_level = derive_alert_level(category, float(data["confidence"]))

    alert_public: AlertPublic | None = None
    async with session_scope() as db:
        alert = await alert_repo.create_alert(
            db,
            clip_id=clip_id,
            organization_id=clip_payload["organization_id"],  # type: ignore[arg-type]
            store_id=UUID(clip_payload["store_id"]) if clip_payload["store_id"] else None,  # type: ignore[arg-type]
            camera_id=UUID(clip_payload["camera_id"]) if clip_payload["camera_id"] else None,  # type: ignore[arg-type]
            category=category,
            confidence=float(data["confidence"]),
            reasoning=str(data["reasoning"]),
            model_name=str(data["model_name"]),
            alert_level=alert_level,
            inference_latency_ms=int(data["inference_latency_ms"]),
            # RAG (docs/19 Phase 4): the AI node embedded `reasoning` at verify
            # time; keep it so staff feedback can spawn a verified_case.
            embedding=(
                [float(x) for x in emb]
                if isinstance((emb := data.get("embedding")), list)
                else None
            ),
        )
        # session_scope auto-commits on exit
        alert_public = AlertPublic.model_validate(alert)
        # BE1 — best-effort Telegram ping (store query + send use this session).
        await alert_notify.notify_alert(db, alert)

    # 4. Push to SSE subscribers (outside DB session)
    if alert_public is not None:
        await get_broker().publish(
            alert_public.organization_id,
            alert_public.model_dump(mode="json"),
        )
        log.info(
            "alert published",
            clip_id=str(clip_id),
            alert_id=str(alert_public.id),
            category=str(category),
            level=str(alert_level),
        )
