"""Config page: env editor + connection tests (PLAN §9.1)."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from app.audit.admin_log import admin_log
from app.auth.clouds import get_endpoints
from app.auth.msal_client import graph_token, invalidate_all, mgmt_token
from app.core.config import atomic_write_env, reload_settings, settings
from app.db.bootstrap import SchemaDriftError, check_schema_status, run_bootstrap
from app.db.engine import dispose_engine, get_engine
from app.web.deps import CurrentPrincipal, RequestId

router = APIRouter(prefix="/config", tags=["config"])

EDITABLE_KEYS = {
    "AZURE_CLOUD", "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
    "M365_REDIRECT_URI",
    "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASS",
    "POLL_INTERVAL_S", "GRAPH_LOOKBACK_HOURS", "MGMT_CONTENT_TYPES",
    "INGEST_ENABLED",
    "WEB_PORT", "TZ",
    "OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
    "OIDC_REDIRECT_URI", "OIDC_REQUIRED_GROUP", "OIDC_USERNAME_CLAIM",
    "LOCAL_LOGIN_ENABLED",
}
SENSITIVE_KEYS = {"AZURE_CLIENT_SECRET", "DB_PASS", "OIDC_CLIENT_SECRET", "APP_SECRET_KEY"}
DB_KEYS = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASS"}
CHECKBOX_KEYS = {"LOCAL_LOGIN_ENABLED", "INGEST_ENABLED"}

# /data is a writable persistent volume; /app/.env is mounted read-only from host.
# GUI saves go to overrides so we never need write access to the host .env.
ENV_PATH = Path(os.environ.get("ENV_FILE_PATH", "/data/m365ai-overrides.env"))


def _read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def _suffix(secret: str, n: int = 4) -> str:
    """Last n chars of a secret for the GUI hint. Never returns full value."""
    if not secret:
        return ""
    s = secret[-n:] if len(secret) >= n else secret
    return f"…{s}"


@router.get("")
async def show(request: Request, _p: CurrentPrincipal):  # noqa: ANN201
    templates = request.app.state.templates
    s = settings()
    # Last-4 hints for sensitive fields (so the operator can verify which
    # secret is active without exposing the full value).
    secret_suffixes = {
        "AZURE_CLIENT_SECRET": _suffix(s.azure_client_secret.get_secret_value()),
        "DB_PASS": _suffix(s.db_pass.get_secret_value()),
        "OIDC_CLIENT_SECRET": _suffix(s.oidc_client_secret.get_secret_value()),
        "APP_SECRET_KEY": _suffix(s.app_secret_key.get_secret_value()),
    }
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "settings": s,
            "secret_suffixes": secret_suffixes,
        },
    )


@router.post("")
async def save(request: Request, principal: CurrentPrincipal, request_id: RequestId):  # noqa: ANN201
    form = dict(await request.form())
    # Checkboxes send nothing when unchecked — explicitly set to "false".
    for k in CHECKBOX_KEYS:
        if k not in form:
            form[k] = "false"
    current = _read_env()
    diff: dict[str, tuple[str | None, str]] = {}
    for k, v in form.items():
        if k not in EDITABLE_KEYS:
            continue
        v = str(v)
        # Treat empty value for sensitive keys as "unchanged"
        if k in SENSITIVE_KEYS and not v:
            continue
        if current.get(k) != v:
            diff[k] = (current.get(k), v)
            current[k] = v
    if diff:
        atomic_write_env(ENV_PATH, current)
        reload_settings()
        invalidate_all()
        db_changed = any(k in diff for k in DB_KEYS)
        if db_changed:
            # Cached engine now points at the old DB. Dispose so the next
            # request rebuilds against the new DSN. The /db-status panel will
            # surface "missing tables" on the new target until Init is clicked.
            dispose_engine()
        action = "secret.rotate" if any(k in SENSITIVE_KEYS for k in diff) else "config.update"
        log_details = {
            "changed": {
                k: ("***" if k in SENSITIVE_KEYS else d[1])
                for k, d in diff.items()
            }
        }
        admin_log(
            get_engine(),
            action=action,
            actor=principal["username"],
            actor_kind=principal["kind"],
            request_id=request_id,
            target=str(ENV_PATH),
            details=log_details,
        )
    return RedirectResponse("/config", status_code=302)


@router.get("/db-status")
async def db_status(_p: CurrentPrincipal):  # noqa: ANN201
    """Non-raising introspection used by the status panel on /config."""
    try:
        return JSONResponse(check_schema_status(get_engine()))
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {
                "db_connected": False,
                "db_error": str(e)[:500],
                "tables_present": {},
                "drift": {},
                "ready": False,
            }
        )


@router.post("/db-init")
async def db_init(principal: CurrentPrincipal, request_id: RequestId):  # noqa: ANN201
    """Manually trigger bootstrap (create missing tables, verify drift)."""
    eng = get_engine()
    try:
        run_bootstrap(eng)
    except SchemaDriftError as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "status": check_schema_status(eng)},
            status_code=400,
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    admin_log(
        eng,
        action="config.update",
        actor=principal["username"],
        actor_kind=principal["kind"],
        request_id=request_id,
        target="schema",
        details={"op": "db_init"},
    )
    return JSONResponse({"ok": True, "status": check_schema_status(eng)})


@router.post("/test/db")
async def test_db(_p: CurrentPrincipal):  # noqa: ANN201
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/test/graph")
async def test_graph(_p: CurrentPrincipal):  # noqa: ANN201
    try:
        endpoints = get_endpoints(settings().azure_cloud)
        with httpx.Client(timeout=10.0) as client:
            r = client.get(
                f"{endpoints['graph_host']}/v1.0/$metadata",
                headers={"Authorization": f"Bearer {graph_token()}"},
            )
        return JSONResponse({"ok": r.status_code < 500, "status": r.status_code})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/test/mgmt")
async def test_mgmt(_p: CurrentPrincipal):  # noqa: ANN201
    try:
        s = settings()
        endpoints = get_endpoints(s.azure_cloud)
        url = (
            f"{endpoints['mgmt_host']}/api/v1.0/{s.azure_tenant_id}"
            "/activity/feed/subscriptions/list"
        )
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {mgmt_token()}"})
        return JSONResponse({"ok": r.status_code < 500, "status": r.status_code})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
