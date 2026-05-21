"""FastAPI app factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.bootstrap import fail_if_drift
from app.db.engine import get_engine
from app.web.deps import NotAuthenticated
from app.web.pages import (
    admin_events as admin_events_page,
    config as config_page,
    discover,
    health,
    login,
    m365,
    mapping,
    runs,
)

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    s = settings()
    if s.oidc_enabled:
        log.info("web.oidc.enabled")
    engine = get_engine()
    fail_if_drift(engine)
    log.info(
        "web.ready",
        host_port=s.web_port,
        container_port=8080,
        cloud=s.azure_cloud,
    )
    yield


def create_app() -> FastAPI:
    s = settings()
    app = FastAPI(title="M365 Audit Ingestor", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.app_secret_key.get_secret_value() or "dev-insecure",
        same_site="lax",
        https_only=bool(s.oidc_enabled),
    )
    app.state.templates = templates

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/runs", status_code=302)

    app.include_router(login.router)
    app.include_router(config_page.router)
    app.include_router(m365.router)
    app.include_router(discover.router)
    app.include_router(mapping.router)
    app.include_router(runs.router)
    app.include_router(admin_events_page.router)
    app.include_router(health.router)

    @app.exception_handler(NotAuthenticated)
    async def _not_auth(request: Request, _exc: NotAuthenticated):
        """Browser → 303 to /login; API client → JSON 401."""
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    @app.exception_handler(Exception)
    async def _all(request: Request, exc: Exception) -> JSONResponse:
        log.error("web.error", path=str(request.url.path), error=str(exc))
        return JSONResponse({"error": "internal"}, status_code=500)

    return app


asgi = create_app()
