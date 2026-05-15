"""AZURE_CLOUD → endpoint lookup (PLAN §4.4 / D-14).

Sovereign tenants must never silently fall back to commercial endpoints.
"""
from __future__ import annotations

from typing import TypedDict


class CloudEndpoints(TypedDict):
    login_authority: str
    graph_host: str
    mgmt_host: str
    admin_consent_host: str


CLOUDS: dict[str, CloudEndpoints] = {
    "commercial": {
        "login_authority": "https://login.microsoftonline.com",
        "graph_host": "https://graph.microsoft.com",
        "mgmt_host": "https://manage.office.com",
        "admin_consent_host": "https://login.microsoftonline.com",
    },
    "gcc-high": {
        "login_authority": "https://login.microsoftonline.us",
        "graph_host": "https://graph.microsoft.us",
        "mgmt_host": "https://manage.office365.us",
        "admin_consent_host": "https://login.microsoftonline.us",
    },
    "dod": {
        "login_authority": "https://login.microsoftonline.us",
        "graph_host": "https://dod-graph.microsoft.us",
        "mgmt_host": "https://manage.protection.apps.mil",
        "admin_consent_host": "https://login.microsoftonline.us",
    },
    "china": {
        "login_authority": "https://login.partner.microsoftonline.cn",
        "graph_host": "https://microsoftgraph.chinacloudapi.cn",
        "mgmt_host": "https://manage.office365.cn",
        "admin_consent_host": "https://login.partner.microsoftonline.cn",
    },
}


def get_endpoints(cloud: str) -> CloudEndpoints:
    if cloud not in CLOUDS:
        raise ValueError(f"unknown AZURE_CLOUD: {cloud}")
    return CLOUDS[cloud]
