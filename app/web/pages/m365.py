"""Microsoft 365 admin-consent flow."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.audit.admin_log import admin_log
from app.auth.consent import consent_url
from app.db.engine import get_engine
from app.web.deps import CurrentPrincipal, RequestId

router = APIRouter(prefix="/m365", tags=["m365"])


@router.get("")
async def page(request: Request, _p: CurrentPrincipal):  # noqa: ANN201
    templates = request.app.state.templates
    return templates.TemplateResponse("m365.html", {"request": request})


@router.post("/authorize")
async def authorize(request: Request, _p: CurrentPrincipal):  # noqa: ANN201
    state = secrets.token_urlsafe(32)
    request.session["m365_consent_state"] = state
    return RedirectResponse(consent_url(state), status_code=302)


@router.get("/callback")
async def callback(  # noqa: ANN201
    request: Request, principal: CurrentPrincipal, request_id: RequestId
):
    state = request.query_params.get("state")
    if state != request.session.get("m365_consent_state"):
        return RedirectResponse("/m365?error=state", status_code=302)
    admin_consent = request.query_params.get("admin_consent", "False")
    tenant = request.query_params.get("tenant", "")
    admin_log(
        get_engine(),
        action="oauth.consent",
        actor=principal["username"],
        actor_kind=principal["kind"],
        request_id=request_id,
        target=tenant,
        details={"admin_consent": admin_consent},
    )
    return RedirectResponse("/m365?ok=1", status_code=302)
