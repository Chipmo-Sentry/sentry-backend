"""FastAPI application entrypoint."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from sentry_backend import __version__
from sentry_backend.db.session import dispose_engine, get_sessionmaker
from sentry_backend.logging_setup import configure_logging, get_logger
from sentry_backend.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
    async def dispatch(  # type: ignore[override]
        self, request: Request, call_next  # type: ignore[no-untyped-def]
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

    return app


app = create_app()
