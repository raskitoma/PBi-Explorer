"""Login: dispatches OIDC or local form (PLAN §9.2)."""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.audit.admin_log import admin_log
from app.auth.local import hash_password, needs_rehash, verify_password
from app.auth.oidc import get_oauth, group_check, username_from_claims
from app.core.config import settings
from app.core.metrics import dashboard_login_failure_total
from app.db import repo
from app.db.engine import get_engine
from app.web.deps import RequestId

router = APIRouter(tags=["auth"])


@router.get("/login")
async def login_get(request: Request):  # noqa: ANN201
    s = settings()
    templates = request.app.state.templates
    if s.oidc_enabled and not s.local_login_enabled:
        return RedirectResponse("/auth/oidc/start", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"oidc": s.oidc_enabled, "local": s.local_login_enabled},
    )


@router.post("/login")
async def login_post(  # noqa: PLR0913
    request: Request,
    request_id: RequestId,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    s = settings()
    if not s.local_login_enabled:
        raise HTTPException(status_code=404)
    eng = get_engine()
    src_ip = request.client.host if request.client else None
    stored: str | None = None
    is_break_glass = False

    if username == s.dashboard_user and s.dashboard_pass_hash:
        stored = s.dashboard_pass_hash
        is_break_glass = s.oidc_enabled
    else:
        user = repo.get_user(eng, username)
        if user is None:
            dashboard_login_failure_total.inc()
            admin_log(
                eng, action="login.fail", actor=username, actor_kind="local",
                request_id=request_id, source_ip=src_ip,
                details={"reason": "unknown_user"},
            )
            raise HTTPException(status_code=401, detail="invalid credentials")
        stored = user["password_hash"]
        is_break_glass = bool(user.get("is_break_glass"))

    if s.oidc_enabled and not is_break_glass:
        raise HTTPException(status_code=403, detail="local login disabled — use SSO")

    if not stored or not verify_password(password, stored):
        dashboard_login_failure_total.inc()
        admin_log(
            eng, action="login.fail", actor=username, actor_kind="local",
            request_id=request_id, source_ip=src_ip,
            details={"reason": "bad_password"},
        )
        raise HTTPException(status_code=401, detail="invalid credentials")

    if needs_rehash(stored):
        new_hash = hash_password(password)
        repo.upsert_user(
            eng, username=username, password_hash=new_hash, is_break_glass=is_break_glass
        )

    request.session["principal"] = {"username": username, "kind": "local", "role": "admin"}
    details = {"reason": "break_glass"} if (s.oidc_enabled and is_break_glass) else None
    admin_log(
        eng, action="login.ok", actor=username, actor_kind="local",
        request_id=request_id, source_ip=src_ip, details=details,
    )
    return RedirectResponse("/runs", status_code=302)


@router.post("/logout")
async def logout(request: Request):  # noqa: ANN201
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@router.get("/auth/oidc/start")
async def oidc_start(request: Request):  # noqa: ANN201
    s = settings()
    if not s.oidc_enabled:
        raise HTTPException(status_code=404)
    oauth = get_oauth()
    state = secrets.token_urlsafe(32)
    request.session["oidc_state"] = state
    return await oauth.idp.authorize_redirect(request, s.oidc_redirect_uri, state=state)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, request_id: RequestId):  # noqa: ANN201
    s = settings()
    if not s.oidc_enabled:
        raise HTTPException(status_code=404)
    oauth = get_oauth()
    eng = get_engine()
    src_ip = request.client.host if request.client else None
    state = request.query_params.get("state")
    if state != request.session.get("oidc_state"):
        admin_log(
            eng, action="login.fail", actor="unknown", actor_kind="oidc",
            request_id=request_id, source_ip=src_ip,
            details={"reason": "oidc_state_mismatch"},
        )
        raise HTTPException(status_code=400)
    token = await oauth.idp.authorize_access_token(request)
    claims = token.get("userinfo") or token.get("id_token_claims") or {}
    username = username_from_claims(claims)
    if not group_check(claims):
        admin_log(
            eng, action="login.fail", actor=username, actor_kind="oidc",
            request_id=request_id, source_ip=src_ip,
            details={"reason": "group_check_failed"},
        )
        raise HTTPException(status_code=403, detail="user not in required group")
    request.session["principal"] = {"username": username, "kind": "oidc", "role": "admin"}
    admin_log(
        eng, action="login.ok", actor=username, actor_kind="oidc",
        request_id=request_id, source_ip=src_ip,
    )
    return RedirectResponse("/runs", status_code=302)
