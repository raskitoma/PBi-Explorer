"""Office 365 Management Activity ingestion (PLAN §5.2)."""
from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx

from app.auth.clouds import get_endpoints
from app.auth.msal_client import invalidate_all, mgmt_token
from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import (
    api_request_total,
    api_retry_total,
    mgmt_subscription_state,
)

log = get_logger(__name__)


def _base() -> str:
    s = settings()
    endpoints = get_endpoints(s.azure_cloud)
    return f"{endpoints['mgmt_host']}/api/v1.0/{s.azure_tenant_id}/activity/feed"


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _request(
    method: str, url: str, *, client: httpx.Client, max_retries: int = 5
) -> httpx.Response:
    for attempt in range(max_retries):
        headers = {"Authorization": f"Bearer {mgmt_token()}"}
        resp = client.request(method, url, headers=headers, timeout=30.0)
        api_request_total.labels(api="mgmt", code=str(resp.status_code)).inc()
        if resp.status_code == 401:
            api_retry_total.labels(api="mgmt", reason="401").inc()
            invalidate_all()
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            wait = _retry_after(resp, default=float(2 ** attempt))
            api_retry_total.labels(
                api="mgmt", reason="429" if resp.status_code == 429 else "5xx"
            ).inc()
            time.sleep(wait)
            continue
        return resp
    raise RuntimeError(f"mgmt: too many retries for {url}")


def ensure_subscriptions(
    content_types: list[str], *, client: httpx.Client
) -> dict[str, bool]:
    """Start subscriptions, idempotent. Sets mgmt_subscription_state metric."""
    base = _base()
    state: dict[str, bool] = {}
    for ct in content_types:
        url = f"{base}/subscriptions/start?contentType={ct}"
        resp = _request("POST", url, client=client)
        if resp.status_code in (200, 201):
            state[ct] = True
            log.info("mgmt.sub.started", content_type=ct)
        elif resp.status_code == 400 and "already" in resp.text.lower():
            state[ct] = True
        elif resp.status_code == 403:
            state[ct] = False
            log.warning("mgmt.sub.forbidden", content_type=ct, body=resp.text[:200])
        else:
            state[ct] = False
            log.warning(
                "mgmt.sub.error", content_type=ct, code=resp.status_code, body=resp.text[:200]
            )
        mgmt_subscription_state.labels(content_type=ct).set(1 if state[ct] else 0)
    return state


def list_content(
    content_type: str,
    *,
    client: httpx.Client,
    start_uri: str | None = None,
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    """Yields (descriptor_pages, next_page_uri). Each descriptor has 'contentUri'."""
    base = _base()
    url: str | None = (
        start_uri or f"{base}/subscriptions/content?contentType={content_type}"
    )
    while url:
        resp = _request("GET", url, client=client)
        if resp.status_code != 200:
            log.warning(
                "mgmt.list.error", code=resp.status_code, body=resp.text[:200]
            )
            break
        descriptors = resp.json() or []
        nxt = resp.headers.get("NextPageUri")
        yield descriptors, nxt
        url = nxt


def fetch_blob(content_uri: str, *, client: httpx.Client) -> list[dict[str, Any]]:
    resp = _request("GET", content_uri, client=client)
    resp.raise_for_status()
    return resp.json() or []
