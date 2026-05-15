"""Prometheus metrics catalog — see PLAN §11.3."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

events_ingested_total = Counter(
    "m365ai_events_ingested_total",
    "Events written post-dedup",
    ["subsource"],
)
events_duplicate_total = Counter(
    "m365ai_events_duplicate_total",
    "Events that hit the dedup unique index",
    ["subsource"],
)
events_canonical_unknown_total = Counter(
    "m365ai_events_canonical_unknown_total",
    "Operation/instance values that fell through to unknown.*",
    ["subsource", "target"],
)
run_duration_seconds = Histogram(
    "m365ai_run_duration_seconds",
    "Wall time of a finalized run",
    ["subsource", "status"],
)
run_status_total = Counter(
    "m365ai_run_status_total",
    "One increment per finalized run",
    ["subsource", "status"],
)
api_request_total = Counter(
    "m365ai_api_request_total",
    "Upstream HTTP status counts",
    ["api", "code"],
)
api_retry_total = Counter(
    "m365ai_api_retry_total",
    "Retries triggered",
    ["api", "reason"],
)
api_token_refresh_total = Counter(
    "m365ai_api_token_refresh_total",
    "Token acquisitions",
    ["api", "outcome"],
)
cursor_lag_seconds = Gauge(
    "m365ai_cursor_lag_seconds",
    "now() − activityDateTime of the most recent ingested event",
    ["feed"],
)
mgmt_subscription_state = Gauge(
    "m365ai_mgmt_subscription_state",
    "1 enabled, 0 disabled",
    ["content_type"],
)
db_pool_in_use = Gauge(
    "m365ai_db_pool_in_use",
    "SQLAlchemy pool checkout count",
)
dashboard_login_failure_total = Counter(
    "m365ai_dashboard_login_failure_total",
    "Login failures, for external WAF alerting",
)
