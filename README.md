# sentry-backend

HTTP API for **Chipmo Sentry** — auth, multi-tenant org/store/camera/alert CRUD, feedback, OpenAPI contract.

Python 3.11 · FastAPI · SQLAlchemy 2.0 async · Postgres · structlog · Apache 2.0

---

## What this service does

- **Identity & multi-tenancy**: User accounts, Organization → Store → Camera hierarchy, role-based access (owner/admin/staff + super-admin)
- **JWT auth**: httpOnly cookie + Bearer fallback; 15 min access + 7 day refresh
- **Clip CRUD**: receive uploaded mp4 clips, dispatch to [sentry-ai](https://github.com/Chipmo-Sentry/sentry-ai) for inference
- **Alert CRUD**: store AI verdicts, expose via REST + SSE real-time push
- **Feedback**: collect staff TP/FP marking, feed M3 auto-learner
- **OpenAPI contract**: `/openapi.json` is the source of truth for [sentry-frontend](https://github.com/Chipmo-Sentry/sentry-frontend) and Go agent codegen

Not in scope here: video ingest (see [sentry-ingest](https://github.com/Chipmo-Sentry/sentry-ingest)), AI inference (see [sentry-ai](https://github.com/Chipmo-Sentry/sentry-ai)).

---

## Quick start

```bash
# 1. Install uv (one-time)
pip install uv

# 2. Sync dependencies into ./.venv
uv sync

# 3. Copy env template, fill in secrets
cp .env.example .env
# Generate JWT_SECRET:          python -c "import secrets; print(secrets.token_urlsafe(48))"
# Generate SERVICE_TOKEN_SECRET: python -c "import secrets; print(secrets.token_urlsafe(48))"
# Generate RTSP_FERNET_KEY:     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 4. Spin up Postgres (Docker example)
docker run -d --name sentry-pg \
  -e POSTGRES_USER=sentry -e POSTGRES_PASSWORD=sentry -e POSTGRES_DB=sentry_dev \
  -p 5432:5432 postgres:16

# 5. Generate + apply initial migration (FIRST RUN ONLY — see "Migrations" below)
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head

# 6. Run the API
uv run uvicorn sentry_backend.main:app --reload
# → http://localhost:8000/healthz
# → http://localhost:8000/docs (Swagger UI)
# → http://localhost:8000/openapi.json (contract source of truth)
```

---

## Project layout

```
src/sentry_backend/
├── main.py                 — FastAPI app, lifespan, middleware
├── settings.py             — pydantic-settings BaseSettings
├── logging_setup.py        — structlog (JSON in prod, pretty in dev)
├── security.py             — JWT, bcrypt, cookies, Fernet, service tokens
├── db/
│   ├── session.py          — async engine, AsyncSessionLocal
│   ├── base.py             — DeclarativeBase, UUIDPrimaryKeyMixin, TimestampMixin
│   └── models/             — ORM (one file per entity)
├── schemas/                — Pydantic request/response (OpenAPI source)
├── deps/                   — Dependency injection (get_db, get_current_user, ...)
├── repository/             — CRUD layer (service-callable)
├── services/               — Business logic
├── api/v1/                 — Versioned routers
└── api/stream.py           — SSE alert push endpoint

alembic/                    — Migration env + version files
tests/{unit,integration}/   — pytest suites
```

5-layer separation: **Schema → ORM → Repository → Service → API**. Routers do not write SQL.

---

## Migrations

Alembic is async-aware and reads the database URL from `settings.database_url` (env), **not** from `alembic.ini`. So secrets never land in the repo.

```bash
# Generate a migration from ORM changes
uv run alembic revision --autogenerate -m "add foo column to bar"

# Inspect the generated file — autogenerate misses some changes (e.g. constraint renames)
# Edit alembic/versions/<id>_<slug>.py manually if needed

# Apply to current DB
uv run alembic upgrade head

# Roll back one step
uv run alembic downgrade -1
```

> **The very first migration is NOT committed in this repo.** Run `alembic revision --autogenerate -m "initial schema"` against a fresh Postgres to generate it, then commit. This avoids shipping a migration that hasn't been verified against a real DB.

---

## Testing

```bash
# All tests
uv run pytest

# Unit only (fast, no DB)
uv run pytest tests/unit/

# Integration (needs real Postgres — DATABASE_URL pointed at a test DB)
uv run pytest tests/integration/

# With coverage report
uv run pytest --cov=sentry_backend --cov-report=html
```

## Lint + type-check

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src/sentry_backend
```

CI runs all three; PR fails if any of them does.

---

## Deployment

Target: **Railway Pro**. Postgres is a Railway add-on.

```bash
# Railway will run uvicorn on $PORT — see Dockerfile (TBD next session)
# Migrations apply on container start: alembic upgrade head && uvicorn ...
```

---

## Related repos

- [sentry-ai](https://github.com/Chipmo-Sentry/sentry-ai) — AI inference (YOLO + VLM)
- [sentry-ingest](https://github.com/Chipmo-Sentry/sentry-ingest) — Video receive
- [sentry-frontend](https://github.com/Chipmo-Sentry/sentry-frontend) — Customer dashboard
- [sentry-ui-kit](https://github.com/Chipmo-Sentry/sentry-ui-kit) — Shared design system

Platform overview: [Sentry-v.3 README](../README.md) (local workspace)
