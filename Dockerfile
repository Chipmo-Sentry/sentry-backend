# syntax=docker/dockerfile:1.7
# ============================================================================
# Stage 1 — builder: install deps into a venv, copy source
# ============================================================================
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Pull `uv` from its official distroless image (fast, no apt step required)
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps WITHOUT installing the project (better layer cache)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Now copy source + install the project itself
COPY src ./src
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ============================================================================
# Stage 2 — runtime: minimal image, only the venv + source + alembic
# ============================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# curl for healthcheck only; ca-certificates for HTTPS to Postgres
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Venv (incl. installed project) from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src ./src

# Alembic config + migration versions
COPY alembic.ini ./
COPY alembic ./alembic

# Storage dir for clip uploads (M1: local fs; M2+: S3/B2)
RUN mkdir -p /app/storage/clips

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8000}/healthz || exit 1

# Run migrations then the API.
# Railway sets PORT dynamically; default 8000 for local docker run.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn sentry_backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
