"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from sentry_backend import __version__
from sentry_backend.api import ws_live
from sentry_backend.api.v1 import admin as admin_v1
from sentry_backend.api.v1 import agents as agents_v1
from sentry_backend.api.v1 import alerts as alerts_v1
from sentry_backend.api.v1 import auth as auth_v1
from sentry_backend.api.v1 import behaviors as behaviors_v1
from sentry_backend.api.v1 import cameras as cameras_v1
from sentry_backend.api.v1 import clips as clips_v1
from sentry_backend.api.v1 import feedback as feedback_v1
from sentry_backend.api.v1 import internal as internal_v1
from sentry_backend.api.v1 import leads as leads_v1
from sentry_backend.api.v1 import stores as stores_v1
from sentry_backend.db.session import dispose_engine, get_sessionmaker
from sentry_backend.logging_setup import configure_logging, get_logger
from sentry_backend.ratelimit import limiter
from sentry_backend.settings import get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = get_logger("sentry_backend.lifespan")
    log.info(
        "starting",
        version=__version__,
        environment=get_settings().environment,
    )
    # P3: re-push camera paths to MediaMTX so the live pipeline recovers after
    # a reboot without manual re-registration. Best-effort — never blocks boot.
    try:
        from sentry_backend.services.mediamtx_sync import rehydrate_paths

        await rehydrate_paths()
    except Exception:  # noqa: BLE001
        log.warning("mediamtx_rehydrate_failed", exc_info=True)
    # First-run super-admin bootstrap from env (idempotent, best-effort).
    try:
        from sentry_backend.services.bootstrap import bootstrap_superadmin

        await bootstrap_superadmin()
    except Exception:  # noqa: BLE001
        log.warning("bootstrap_superadmin_failed", exc_info=True)
    yield
    log.info("stopping")
    await dispose_engine()


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("x-request-id", uuid4().hex)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["x-request-id"] = request_id
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    is_prod = settings.environment == "production"

    # Interactive API docs + schema are useful in dev but are unnecessary
    # attack/recon surface on a public production deployment.
    app = FastAPI(
        title="Chipmo Sentry Backend",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )

    # LD.2: per-IP rate limiting (currently only the public /leads endpoint).
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.state.limiter = limiter
    # slowapi's handler signature is narrower than Starlette's generic
    # Exception handler type; safe to register.
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # CORS must fail CLOSED with credentials. A wildcard origin combined with
    # allow_credentials is the classic credentialed-CORS hole — and in prod a
    # missing ALLOWED_ORIGINS should be a hard error, never "allow everyone".
    if not settings.allowed_origins:
        if is_prod:
            raise RuntimeError(
                "ALLOWED_ORIGINS must be set in production (refusing to start "
                "with an open credentialed CORS policy)."
            )
        # dev convenience only
        cors_origins = ["http://localhost:3000", "http://localhost:5173"]
    else:
        cors_origins = settings.allowed_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIdMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    app.include_router(auth_v1.router)
    app.include_router(stores_v1.router)
    app.include_router(cameras_v1.router)
    app.include_router(clips_v1.router)
    app.include_router(alerts_v1.router)
    app.include_router(feedback_v1.router)
    app.include_router(leads_v1.router)
    app.include_router(internal_v1.router)
    app.include_router(admin_v1.router)
    app.include_router(agents_v1.router)
    app.include_router(behaviors_v1.router)
    app.include_router(ws_live.router)

    return app


app = create_app()
