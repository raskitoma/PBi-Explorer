"""Runs list + drill-in."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.db import repo
from app.db.engine import get_engine
from app.web.deps import CurrentPrincipal

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
async def page(  # noqa: ANN201
    request: Request,
    _p: CurrentPrincipal,
    source: str | None = Query(None),
    since: str | None = Query(None),
):
    templates = request.app.state.templates
    since_d: date | None = date.fromisoformat(since) if since else None
    rows = repo.list_runs(get_engine(), source=source, since=since_d, limit=200)
    return templates.TemplateResponse(
        "runs.html", {"request": request, "rows": rows, "source": source}
    )


@router.get("/{run_id}")
async def detail(request: Request, _p: CurrentPrincipal, run_id: int):  # noqa: ANN201
    templates = request.app.state.templates
    eng = get_engine()
    with eng.connect() as conn:
        run_row = (
            conn.execute(
                text("SELECT * FROM z_audit_logs_efk_runs WHERE id=:i"),
                {"i": run_id},
            )
            .mappings()
            .first()
        )
        samples = (
            conn.execute(
                text(
                    "SELECT timestamp, operation, instance, user_name, dedup_hash "
                    "FROM z_audit_logs_efk WHERE ingest_run_id=:i "
                    "ORDER BY id DESC LIMIT 10"
                ),
                {"i": run_id},
            )
            .mappings()
            .all()
        )
    return templates.TemplateResponse(
        "runs_detail.html",
        {
            "request": request,
            "run": dict(run_row) if run_row else None,
            "samples": [dict(s) for s in samples],
        },
    )
