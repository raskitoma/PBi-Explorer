"""dedup_hash = SHA-256(source|subsource|external_id|iso_ts)  (PLAN D-07)."""
from __future__ import annotations

import hashlib
from datetime import datetime


def compute_dedup_hash(*, source: str, subsource: str, external_id: str, ts: datetime) -> str:
    ms_ts = ts.replace(microsecond=(ts.microsecond // 1000) * 1000)
    iso = ms_ts.isoformat()
    raw = f"{source}|{subsource}|{external_id}|{iso}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
