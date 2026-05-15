"""Microsoft Graph audit poller (PLAN §5.1)."""
from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx

from app.auth.clouds import get_endpoints
from app.auth.msal_client import graph_token, invalidate_all
from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import api_request_total, api_retry_total
from app.core.timeutils import hours_ago

log = get_logger(__name__)

FEED_PATHS = {
    "graph.directoryAudits": "/auditLogs/directoryAudits",
    "graph.signIns": "/auditLogs/signIns",
    "graph.provisioning": "/auditLogs/provisioning",
}


def initial_url(feed: str, lookback_hours: int) -> str:
    endpoints = get_endpoints(settings().azure_cloud)
    path = FEED_PATHS[feed]
    since = hours_ago(lookback_hours).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{endpoints['graph_host']}/v1.0{path}?$filter=activityDateTime ge {since}"


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def fetch_page(
    url: str, *, client: httpx.Client, max_retries: int = 5
) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            headers = {"Authorization": f"Bearer {graph_token()}"}
            resp = client.get(url, headers=headers, timeout=30.0)
            api_request_total.labels(api="graph", code=str(resp.status_code)).inc()
            if resp.status_code == 401:
                api_retry_total.labels(api="graph", reason="401").inc()
                invalidate_all()
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                wait = _retry_after(resp, default=float(2 ** attempt))
                api_retry_total.labels(
                    api="graph",
                    reason="429" if resp.status_code == 429 else "5xx",
                ).inc()
                log.warning(
                    "graph.retry",
                    code=resp.status_code,
                    wait=wait,
                    attempt=attempt,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as e:
            api_retry_total.labels(api="graph", reason="network").inc()
            log.warning("graph.network", error=str(e), attempt=attempt)
            time.sleep(2 ** attempt)
            last = e
    raise RuntimeError(f"graph: too many retries for {url}: {last}")


def walk(
    feed: str, start_url: str, *, client: httpx.Client
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    """Yields (events, next_link). next_link is None when the page chain ends."""
    url: str | None = start_url
    _ = feed  # currently unused; kept for future per-feed shaping
    while url:
        page = fetch_page(url, client=client)
        events = page.get("value", []) or []
        nxt = page.get("@odata.nextLink") or page.get("@odata.deltaLink")
        yield events, nxt
        url = nxt
