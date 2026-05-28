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
from sentry_backend.api.v1 import alerts as alerts_v1
from sentry_backend.api.v1 import auth as auth_v1
from sentry_backend.api.v1 import behaviors as behaviors_v1
from sentry_backend.api.v1 import cameras as cameras_v1
from sentry_backend.api.v1 import clips as clips_v1
from sentry_backend.api.v1 import feedback as feedback_v1
from sentry_backend.api.v1 import internal as internal_v1
from sentry_backend.api.v1 import stores as stores_v1
from sentry_backend.db.session import dispose_engine, get_sessionmaker
from sentry_backend.logging_setup import configure_logging, get_logger
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
    app = FastAPI(
        title="Chipmo Sentry Backend",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins or ["*"],
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
    app.include_router(internal_v1.router)
    app.include_router(admin_v1.router)
    app.include_router(behaviors_v1.router)
    app.include_router(ws_live.router)

    return app


app = create_app()
