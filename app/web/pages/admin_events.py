"""Read-only admin events page (PLAN §3.7)."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.db import repo
from app.db.engine import get_engine
from app.web.deps import CurrentPrincipal

router = APIRouter(prefix="/admin-events", tags=["admin"])


@router.get("")
async def page(  # noqa: ANN201
    request: Request,
    _p: CurrentPrincipal,
    actor: str | None = Query(None),
    action: str | None = Query(None),
):
    templates = request.app.state.templates
    rows = repo.list_admin_events(get_engine(), actor=actor, action=action, limit=200)
    return templates.TemplateResponse(
        "admin_events.html",
        {"request": request, "rows": rows, "actor": actor, "action": action},
    )
