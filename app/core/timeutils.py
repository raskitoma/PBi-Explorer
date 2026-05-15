"""Time helpers. We always store UTC."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dateutil import parser as dt_parser


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_iso(s: str) -> datetime:
    dt = dt_parser.isoparse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_to_dt3(s: str) -> datetime:
    """ISO-8601 → UTC datetime clamped to millisecond resolution (DATETIME(3))."""
    dt = parse_iso(s)
    return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


def hours_ago(h: int) -> datetime:
    return utcnow() - timedelta(hours=h)
