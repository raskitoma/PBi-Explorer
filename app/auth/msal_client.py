"""MSAL client-credentials for Graph + Mgmt (PLAN §4.1, §4.2)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

import msal

from app.auth.clouds import get_endpoints
from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import api_token_refresh_total

log = get_logger(__name__)


@dataclass
class Token:
    value: str
    expires_at: float

    def is_valid(self, skew: int = 90) -> bool:
        return time.time() + skew < self.expires_at


class TokenCache:
    """Thread-safe in-memory token cache, one entry per scope."""

    def __init__(self) -> None:
        self._tokens: dict[str, Token] = {}
        self._lock = Lock()

    def get(self, scope: str, *, api_label: str) -> str:
        with self._lock:
            tok = self._tokens.get(scope)
            if tok and tok.is_valid():
                return tok.value
            new = _acquire_token(scope, api_label=api_label)
            self._tokens[scope] = new
            return new.value

    def peek(self, scope: str) -> Token | None:
        with self._lock:
            return self._tokens.get(scope)

    def invalidate(self, scope: str) -> None:
        with self._lock:
            self._tokens.pop(scope, None)

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()


def _acquire_token(scope: str, *, api_label: str) -> Token:
    s = settings()
    endpoints = get_endpoints(s.azure_cloud)
    authority = f"{endpoints['login_authority']}/{s.azure_tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id=s.azure_client_id,
        client_credential=s.azure_client_secret.get_secret_value(),
        authority=authority,
    )
    result = app.acquire_token_for_client(scopes=[scope])
    if "access_token" not in result:
        api_token_refresh_total.labels(api=api_label, outcome="error").inc()
        log.error(
            "auth.token.error",
            scope=scope,
            error=result.get("error"),
            description=result.get("error_description"),
        )
        raise RuntimeError(f"token acquisition failed: {result.get('error')}")
    api_token_refresh_total.labels(api=api_label, outcome="ok").inc()
    log.info("auth.token.ok", api=api_label, expires_in=result.get("expires_in"))
    return Token(
        value=str(result["access_token"]),
        expires_at=time.time() + int(result.get("expires_in", 3600)),
    )


_cache = TokenCache()


def graph_token() -> str:
    endpoints = get_endpoints(settings().azure_cloud)
    return _cache.get(f"{endpoints['graph_host']}/.default", api_label="graph")


def mgmt_token() -> str:
    endpoints = get_endpoints(settings().azure_cloud)
    return _cache.get(f"{endpoints['mgmt_host']}/.default", api_label="mgmt")


def graph_token_peek() -> Token | None:
    endpoints = get_endpoints(settings().azure_cloud)
    return _cache.peek(f"{endpoints['graph_host']}/.default")


def mgmt_token_peek() -> Token | None:
    endpoints = get_endpoints(settings().azure_cloud)
    return _cache.peek(f"{endpoints['mgmt_host']}/.default")


def invalidate_all() -> None:
    _cache.clear()
