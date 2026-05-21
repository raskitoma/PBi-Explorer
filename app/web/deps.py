"""Common dependencies: session auth + request_id."""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine

from app.db.engine import get_engine


def request_id(request: Request) -> str:
    return request.headers.get("traceparent") or str(uuid.uuid4())


def current_principal(request: Request) -> dict[str, Any]:
    p = request.session.get("principal")
    if not p:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return p


def db_engine() -> Engine:
    return get_engine()


CurrentPrincipal = Annotated[dict[str, Any], Depends(current_principal)]
RequestId = Annotated[str, Depends(request_id)]
DBEngine = Annotated[Engine, Depends(db_engine)]
