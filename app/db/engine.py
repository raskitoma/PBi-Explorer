"""SQLAlchemy engine factory. One engine per process."""
from __future__ import annotations

from sqlalchemy import Engine, create_engine

from app.core.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings().db_dsn,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=3,
            future=True,
        )
    return _engine


def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
