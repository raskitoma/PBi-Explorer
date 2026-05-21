"""Read-only sampler (PLAN §6). Never writes events to DB."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth.clouds import get_endpoints
from app.auth.msal_client import graph_token, mgmt_token
from app.core.config import settings
from app.ingest.graph import FEED_PATHS
from app.web.deps import CurrentPrincipal

router = APIRouter(prefix="/discover", tags=["discover"])


@router.get("")
async def page(request: Request, _p: CurrentPrincipal):  # noqa: ANN201
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "discover.html", {})


@router.get("/sample")
async def sample(  # noqa: ANN201
    _p: CurrentPrincipal,
    subsource: str = Query(...),
    n: int = Query(50, ge=1, le=500),
):
    s = settings()
    endpoints = get_endpoints(s.azure_cloud)
    events: list[dict[str, Any]] = []
    if subsource in FEED_PATHS:
        url = f"{endpoints['graph_host']}/v1.0{FEED_PATHS[subsource]}?$top={n}"
        with httpx.Client(timeout=30.0) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {graph_token()}"})
            r.raise_for_status()
            events = (r.json().get("value") or [])[:n]
    elif subsource.startswith("mgmt."):
        ct = "Audit." + subsource.split(".", 1)[1]
        url = (
            f"{endpoints['mgmt_host']}/api/v1.0/{s.azure_tenant_id}"
            f"/activity/feed/subscriptions/content?contentType={ct}"
        )
        with httpx.Client(timeout=30.0) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {mgmt_token()}"})
            r.raise_for_status()
            descriptors = (r.json() or [])[:5]
            for d in descriptors:
                if len(events) >= n:
                    break
                rr = c.get(d["contentUri"], headers={"Authorization": f"Bearer {mgmt_token()}"})
                rr.raise_for_status()
                events.extend(rr.json() or [])
            events = events[:n]
    else:
        return JSONResponse({"error": f"unknown subsource: {subsource}"}, status_code=400)

    return JSONResponse(
        {"count": len(events), "schema": _infer_schema(events), "sample": events[:5]}
    )


def _infer_schema(events: list[dict]) -> dict[str, dict]:
    seen: dict[str, dict] = defaultdict(
        lambda: {"types": set(), "sample": None, "nulls": 0, "count": 0}
    )

    def walk(prefix: str, val: Any) -> None:
        if isinstance(val, dict):
            for k, v in val.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
            return
        if isinstance(val, list):
            seen[prefix]["count"] += 1
            seen[prefix]["types"].add("array")
            if val:
                walk(prefix + "[*]", val[0])
            return
        seen[prefix]["count"] += 1
        if val is None:
            seen[prefix]["nulls"] += 1
            seen[prefix]["types"].add("null")
        else:
            seen[prefix]["types"].add(type(val).__name__)
            if seen[prefix]["sample"] is None:
                seen[prefix]["sample"] = val

    for ev in events:
        walk("", ev)

    out: dict[str, dict] = {}
    for path, info in seen.items():
        out[path] = {
            "types": sorted(info["types"]),
            "sample": info["sample"],
            "null_rate": round(info["nulls"] / info["count"], 3) if info["count"] else 0.0,
        }
    return out
