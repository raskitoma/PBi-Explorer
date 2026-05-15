"""Single helper for writing z_m365ai_admin_events (PLAN §3.7)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Engine

from app.db import repo


def admin_log(
    engine: Engine,
    *,
    action: str,
    actor: str,
    actor_kind: str = "local",
    target: str | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    repo.write_admin_event(
        engine,
        actor=actor,
        actor_kind=actor_kind,
        action=action,
        target=target,
        request_id=request_id,
        source_ip=source_ip,
        details=details,
    )
