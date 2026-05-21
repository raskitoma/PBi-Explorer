"""/healthz, /readyz, /metrics."""
from __future__ import annotations

import time

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.auth.msal_client import graph_token_peek, mgmt_token_peek
from app.core.config import settings
from app.db.engine import get_engine

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():  # noqa: ANN201
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():  # noqa: ANN201
    s = settings()
    issues: list[str] = []
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        issues.append(f"db:{e}")

    # Token freshness only enforced once tokens have ever been acquired.
    # First-boot warmup is allowed.
    if s.azure_client_id and s.azure_client_secret.get_secret_value():
        for peek in (graph_token_peek, mgmt_token_peek):
            tok = peek()
            if tok is not None and tok.expires_at - time.time() < 0:
                issues.append("token-stale")
                break

    if issues:
        return Response(",".join(issues), status_code=503, media_type="text/plain")
    return {"status": "ready"}


@router.get("/metrics")
async def metrics():  # noqa: ANN201
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
