"""Common dependencies: session auth + request_id."""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import Engine

from app.db.engine import get_engine


class NotAuthenticated(Exception):
    """Raised when an auth-required route is hit without a valid session.

    Handled in app.web.__init__ so HTML clients get a 303 redirect to /login
    while API clients still get a JSON 401.
    """


def request_id(request: Request) -> str:
    return request.headers.get("traceparent") or str(uuid.uuid4())


def current_principal(request: Request) -> dict[str, Any]:
    p = request.session.get("principal")
    if not p:
        raise NotAuthenticated()
    return p


def db_engine() -> Engine:
    return get_engine()


CurrentPrincipal = Annotated[dict[str, Any], Depends(current_principal)]
RequestId = Annotated[str, Depends(request_id)]
DBEngine = Annotated[Engine, Depends(db_engine)]
