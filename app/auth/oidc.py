"""OIDC PKCE flow via authlib (PLAN §9.2)."""
from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth

from app.core.config import settings

_oauth: OAuth | None = None


def get_oauth() -> OAuth:
    global _oauth
    if _oauth is None:
        s = settings()
        if not s.oidc_issuer_url:
            raise RuntimeError("OIDC not configured")
        oauth = OAuth()
        oauth.register(
            name="idp",
            client_id=s.oidc_client_id,
            client_secret=s.oidc_client_secret.get_secret_value(),
            server_metadata_url=(
                f"{s.oidc_issuer_url.rstrip('/')}/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": "openid profile email groups"},
        )
        _oauth = oauth
    return _oauth


def username_from_claims(claims: dict[str, Any]) -> str:
    s = settings()
    for k in (s.oidc_username_claim, "email", "sub"):
        if k and claims.get(k):
            return str(claims[k])
    raise RuntimeError("no usable username claim")


def group_check(claims: dict[str, Any]) -> bool:
    s = settings()
    if not s.oidc_required_group:
        return True
    groups = claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return s.oidc_required_group in groups
