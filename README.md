# sentry-backend

The HTTP control plane for **Chipmo Sentry** — an AI shoplifting-detection platform for Mongolian retail.
This service owns identity, multi-tenancy, the camera/alert data model, real-time fan-out, and the
OpenAPI contract that every other repo builds against.

Python 3.11 · FastAPI · SQLAlchemy 2.0 (async) · Postgres · Alembic · structlog · Apache-2.0

---

## What this service does

- **Identity & multi-tenancy** — User accounts; `Organization → Store → Camera` hierarchy; org roles
  (owner / admin / staff) plus a platform-wide **super-admin** flag.
- **Auth** — JWT HS256 in an httpOnly cookie (`sentry_access`) with a Bearer fallback; 15-min access +
  7-day refresh. Distinct token *audiences* for **users**, **agents** (the PC relay), **AI nodes** (the
  GPU box), **service** calls (sentry-ai → backend), and short-lived **stream** tokens (camera WHEP/HLS).
- **Camera & store CRUD** — RTSP URLs encrypted at rest (Fernet); MediaMTX paths provisioned/deprovisioned
  on the AI host as cameras are added/edited/removed.
- **Clip pipeline** — receive uploaded mp4 (sha256-deduped), dispatch to [sentry-ai](https://github.com/Chipmo-Sentry/sentry-ai)
  for VLM verification as a fire-and-forget background task, persist the resulting **Alert**.
- **Live pipeline glue** — accept per-frame behaviour metadata from the AI worker and fan it out to
  browsers over WebSocket; detect threshold breaches and trigger the cut→verify→alert flow.
- **Real-time** — per-org **SSE** alert stream + per-camera **WebSocket** live-metadata stream, both via
  in-process brokers.
- **Feedback & RAG** — staff TP/FP marking feeds a retrieval loop (`verified_cases` + embeddings) that
  gives the VLM few-shot context for future clips at the same store.
- **Agent & AI-node onboarding** — 6-digit pairing codes mint scoped JWTs; heartbeats track liveness and
  push central config (provider / frame-skip / enabled) back to nodes.
- **Admin & growth** — super-admin org/user management, alert + feedback analytics, AI-node telemetry
  time-series, behaviour-criteria catalog, and demo-request **lead** capture (with Telegram notify).
- **Notifications** — Telegram pings for actionable alerts (per-store `telegram_chat_id` with a global
  fallback) and for new leads.

**Out of scope here:** video ingest (see [sentry-ingest](https://github.com/Chipmo-Sentry/sentry-ingest)),
AI inference (see [sentry-ai](https://github.com/Chipmo-Sentry/sentry-ai)).

---

## Quick start

```bash
# 1. Install uv (one-time)
pip install uv

# 2. Sync dependencies into ./.venv
uv sync

# 3. Copy env template, fill in secrets
cp .env.example .env
#   JWT_SECRET / SERVICE_TOKEN_SECRET:  python -c "import secrets; print(secrets.token_urlsafe(48))"
#   RTSP_FERNET_KEY:                    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 4. Spin up Postgres (Docker example)
docker run -d --name sentry-pg \
  -e POSTGRES_USER=sentry -e POSTGRES_PASSWORD=sentry -e POSTGRES_DB=sentry_dev \
  -p 5432:5432 postgres:16

# 5. Apply migrations
uv run alembic upgrade head

# 6. (optional) seed a dev super-admin + sample data
uv run python scripts/seed_dev.py

# 7. Run the API
uv run uvicorn sentry_backend.main:app --reload
#  → http://localhost:8000/healthz
#  → http://localhost:8000/docs          (Swagger UI)
#  → http://localhost:8000/openapi.json   (contract — consumed by the web apps)
```

A super-admin can also be bootstrapped at boot from env: set `BOOTSTRAP_SUPERADMIN_EMAIL` /
`BOOTSTRAP_SUPERADMIN_PASSWORD` and the user is created on startup if missing.

---

## Project layout

```
src/sentry_backend/
├── main.py                 — FastAPI app, lifespan (bootstrap, MediaMTX rehydrate, heartbeat), middleware
├── settings.py             — pydantic-settings BaseSettings (all env vars)
├── security.py             — JWT encode/decode, bcrypt, cookies, Fernet, service/stream tokens
├── logging_setup.py        — structlog (JSON in prod, pretty in dev)
├── ratelimit.py            — slowapi limiter singleton
├── db/
│   ├── session.py          — async engine, AsyncSessionLocal, session_scope
│   ├── base.py             — DeclarativeBase, UUIDPrimaryKeyMixin, TimestampMixin
│   └── models/             — ORM, one file per entity (user, org, store, camera, clip, alert,
│                             feedback, agent, ai_node, invitation, lead, rag_case, app_config)
├── schemas/                — Pydantic request/response models (the OpenAPI source of truth)
├── deps/                   — DI: get_db, get_current_user, tenancy, agent_auth, ai_node_auth, service
├── repository/             — CRUD layer (the only place that writes SQL)
├── services/               — business logic (auth, alert_broker, live_broker, ai_service, alert_notify,
│                             lead_notify, clip_cutter, threshold_handler, mediamtx_sync, bootstrap, …)
├── api/v1/                 — versioned routers (see below)
└── api/ws_live.py          — WebSocket /ws/live/{camera_id}

alembic/versions/           — 13 migrations (initial schema → ai-node metrics → invitations → …)
scripts/                    — seed_dev.py, check_live_alerts.py
tests/{unit,integration}/   — pytest suites
```

**Five-layer separation:** `Schema → ORM → Repository → Service → API`. Routers never write SQL; services
never touch FastAPI request objects.

---

## API surface

All routes are under `/api/v1`. Grouped by router:

| Router | Audience | Key routes |
|---|---|---|
| **auth** | user | `POST /auth/login` · `/logout` · `/refresh` · `GET /auth/me` |
| **stores** | user (admin to write) | full CRUD `/stores` |
| **cameras** | user (admin to write) | full CRUD `/cameras` · `GET /cameras/{id}/stream-token` |
| **clips** | user | `POST /clips` (multipart upload → triggers verify) · list · get · `/download` |
| **alerts** | user | `GET /alerts` · `GET /alerts/stream` (SSE) · `GET /alerts/{id}` |
| **feedback** | user | `POST /feedback` (TP/FP/unclear → RAG loop) |
| **org_team** | owner/admin | `/org/members` · `/org/invitations` CRUD · `POST /org/accept-invite` · lock/unlock member |
| **agents** | user + agent | admin: pairing codes, list, revoke · agent: `pair`, `/agent/cameras`, `/agent/stream-config`, `/agent/heartbeat` |
| **ai_nodes** | ai-node | `POST /ai-nodes/pair` · `POST /ai-nodes/heartbeat` (telemetry in, config out) |
| **behaviors** | public read / super-admin write | `GET /behaviors` · PATCH weights+thresholds · add/edit/delete dimensions |
| **admin** | super-admin | `/admin/stats` · `/admin/analytics/{alerts,feedback}` · orgs · users · leads · ai-nodes mgmt+metrics |
| **leads** | public | `POST /leads` (honeypot + rate-limited demo-request capture) |
| **internal** | service / ai-node | `/internal/mediamtx-auth` · `/internal/alerts` · `/internal/live-metadata` · `/internal/rag/*` |
| **ws_live** | user | `WebSocket /ws/live/{camera_id}` (live overlay metadata) |
| **health** | — | `GET /healthz` (DB ping + version) |

`/openapi.json` is the canonical contract — `sentry-frontend` and `sentry-superadmin` codegen their
TypeScript types from it and fail CI on drift.

## Data model

14 entities. Core surveillance chain: **User** ↔ **Organization** (via **OrganizationMember**) →
**Store** → **Camera** → **Clip** → **Alert** ← **Feedback**. Supporting entities: **Agent** +
**AgentPairingCode** (PC relay onboarding), **AiNode** + **AiNodePairingCode** + **AiNodeMetric**
(GPU box telemetry time-series), **Invitation** (org team invites), **Lead** (demo requests),
**VerifiedCase** (RAG embeddings), **AppConfig** (KV store for the tunable behaviour catalog).
Full ERD: [docs/09-DATA-MODEL.md](../docs/09-DATA-MODEL.md).

---

## Migrations

Alembic is async-aware and reads the database URL from `settings.database_url` (env), **not** from
`alembic.ini` — so secrets never land in the repo.

```bash
uv run alembic revision --autogenerate -m "add foo to bar"   # generate from ORM changes
uv run alembic upgrade head                                   # apply
uv run alembic downgrade -1                                   # roll back one step
```

> Autogenerate misses some changes (constraint renames, server defaults, enum edits). Always read the
> generated file in `alembic/versions/` before committing. The container runs `alembic upgrade head`
> automatically on deploy.

---

## Testing, lint, type-check

```bash
uv run pytest                       # all
uv run pytest tests/unit/           # fast, no DB
uv run pytest tests/integration/    # needs a real Postgres (DATABASE_URL)
uv run pytest --cov=sentry_backend --cov-report=html

uv run ruff format .
uv run ruff check .
uv run mypy src/sentry_backend      # strict
```

CI (`.github/workflows/ci.yml`) runs ruff (format + check) → mypy strict → `pytest tests/unit/` on
every push/PR; a separate `railway-deploy.yml` redeploys on push to `main`.

---

## Operational notes

- **Single replica only.** The alert broker (SSE) and live broker (WebSocket) are **in-process**
  pub/sub. `railway.toml` pins `numReplicas = 1`. Scaling out is an M2+ task that requires moving both
  brokers to Redis.
- **Cloud vs LAN topology** is a single switch: set `AGENT_STREAM_PUSH_URL` and the whole system flips
  from "MediaMTX pulls cameras on the LAN" to "agents push to a central relay" (cameras publish, agents
  are told to push, MediaMTX paths go into publish mode).
- **MediaMTX rehydrate** runs on startup so the live pipeline recovers all camera paths after a reboot.

---

## Deployment

Target: **Railway** (Dockerfile + `railway.toml`). Postgres is a Railway addon (`DATABASE_URL` auto-wired).

1. New Railway project from this repo (auto-detects the Dockerfile).
2. Add the **Postgres** addon.
3. Set env vars in the dashboard: `JWT_SECRET`, `SERVICE_TOKEN_SECRET`, `RTSP_FERNET_KEY`,
   `ALLOWED_ORIGINS` (comma-separated web-app origins), `ENVIRONMENT=production`, `LOG_LEVEL=INFO`,
   and — for live + notifications — `SENTRY_AI_URL`, `MEDIAMTX_API_URL`, `LIVE_METADATA_SHARED_SECRET`,
   `TELEGRAM_BOT_TOKEN`. See `.env.example` for the complete list with purposes.
4. Deploy. Container `CMD` runs `alembic upgrade head` then starts uvicorn; healthcheck is `/healthz`.

Local Docker smoke test:

```bash
docker build -t sentry-backend:dev .
docker run --rm -p 8000:8000 --env-file .env sentry-backend:dev
curl http://localhost:8000/healthz
```

---

## Related repos

- [sentry-ai](https://github.com/Chipmo-Sentry/sentry-ai) — VLM verify + live behaviour worker
- [sentry-ingest](https://github.com/Chipmo-Sentry/sentry-ingest) — MediaMTX video ingest
- [sentry-frontend](https://github.com/Chipmo-Sentry/sentry-frontend) — customer dashboard
- [sentry-superadmin](https://github.com/Chipmo-Sentry/sentry-superadmin) — platform admin panel
- [sentry-ui-kit](https://github.com/Chipmo-Sentry/sentry-ui-kit) — shared design system

Platform overview: [Sentry-v.3 README](../README.md).
