"""Mapping CRUD with versioning + coverage gauge (PLAN §3.4.3, §3.6)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine, text

from app.audit.admin_log import admin_log
from app.db import repo
from app.db.engine import get_engine
from app.ingest.normalizer import reload_canonical
from app.web.deps import CurrentPrincipal, RequestId

router = APIRouter(prefix="/mapping", tags=["mapping"])

SUBSOURCES = [
    "graph.directoryAudits",
    "graph.signIns",
    "graph.provisioning",
    "mgmt.SharePoint",
    "mgmt.Exchange",
    "mgmt.AzureActiveDirectory",
    "mgmt.General",
]


def _coverage(engine: Engine, hours: int = 24) -> dict[str, float | int]:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    with engine.connect() as conn:
        total = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM z_audit_logs_efk "
                    "WHERE source='Microsoft365' AND timestamp >= :t"
                ),
                {"t": cutoff},
            ).scalar_one()
        )
        unknown = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM z_audit_logs_efk "
                    "WHERE source='Microsoft365' AND timestamp >= :t "
                    "  AND operation LIKE 'unknown.%'"
                ),
                {"t": cutoff},
            ).scalar_one()
        )
    pct = ((total - unknown) / total * 100) if total else 100.0
    return {"total": total, "unknown": unknown, "pct": round(pct, 1)}


@router.get("")
async def page(request: Request, _p: CurrentPrincipal):  # noqa: ANN201
    templates = request.app.state.templates
    subsource = request.query_params.get("subsource", SUBSOURCES[0])
    eng = get_engine()
    rules = repo.get_active_rules(eng, subsource)
    coverage = _coverage(eng)
    return templates.TemplateResponse(
        "mapping.html",
        {
            "request": request,
            "rules": rules,
            "subsource": subsource,
            "subsources": SUBSOURCES,
            "coverage": coverage,
        },
    )


@router.post("/edit")
async def edit_rule(  # noqa: ANN201, PLR0913
    request: Request,
    principal: CurrentPrincipal,
    request_id: RequestId,
    subsource: str = Form(...),
    target_column: str = Form(...),
    source_jsonpath: str = Form(...),
    transform: str = Form(""),
):
    eng = get_engine()
    new_id = repo.upsert_rule_version(
        eng,
        subsource=subsource,
        target_column=target_column,
        source_jsonpath=source_jsonpath,
        transform=transform or None,
        actor=principal["username"],
    )
    reload_canonical()
    admin_log(
        eng,
        action="mapping.edit",
        actor=principal["username"],
        actor_kind=principal["kind"],
        request_id=request_id,
        target=f"mapping_rule:{new_id}",
        details={
            "subsource": subsource,
            "target_column": target_column,
            "source_jsonpath": source_jsonpath,
            "transform": transform,
        },
    )
    return RedirectResponse(f"/mapping?subsource={subsource}", status_code=302)
