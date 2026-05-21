from __future__ import annotations

from datetime import UTC, datetime

from app.ingest.dedup import compute_dedup_hash


def test_dedup_stable() -> None:
    ts = datetime(2026, 5, 14, 10, 30, 0, tzinfo=UTC)
    h1 = compute_dedup_hash(
        source="Microsoft365", subsource="graph.directoryAudits", external_id="abc", ts=ts
    )
    h2 = compute_dedup_hash(
        source="Microsoft365", subsource="graph.directoryAudits", external_id="abc", ts=ts
    )
    assert h1 == h2
    assert len(h1) == 64
    int(h1, 16)  # valid hex


def test_dedup_differs_by_id() -> None:
    ts = datetime(2026, 5, 14, 10, 30, 0, tzinfo=UTC)
    a = compute_dedup_hash(source="Microsoft365", subsource="x", external_id="a", ts=ts)
    b = compute_dedup_hash(source="Microsoft365", subsource="x", external_id="b", ts=ts)
    assert a != b


def test_dedup_truncates_microseconds_to_ms() -> None:
    a = datetime(2026, 5, 14, 10, 30, 0, 123456, tzinfo=UTC)
    b = datetime(2026, 5, 14, 10, 30, 0, 123999, tzinfo=UTC)
    # Both round down to 123 ms
    ha = compute_dedup_hash(source="x", subsource="y", external_id="id", ts=a)
    hb = compute_dedup_hash(source="x", subsource="y", external_id="id", ts=b)
    assert ha == hb
