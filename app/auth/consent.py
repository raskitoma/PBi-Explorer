"""Admin-consent URL builder for one-time tenant authorization."""
from __future__ import annotations

from urllib.parse import urlencode

from app.auth.clouds import get_endpoints
from app.core.config import settings


def consent_url(state: str) -> str:
    s = settings()
    endpoints = get_endpoints(s.azure_cloud)
    base = f"{endpoints['admin_consent_host']}/{s.azure_tenant_id}/adminconsent"
    params = {
        "client_id": s.azure_client_id,
        "redirect_uri": s.m365_redirect_uri,
        "state": state,
    }
    return f"{base}?{urlencode(params)}"
