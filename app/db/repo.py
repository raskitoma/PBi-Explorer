"""SQL repository — one function per PLAN concern.

Karpathy: each function does one thing; SQL is explicit; no ORM models for
tables we don't shape.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import Engine, text

from app.core.logging import get_logger
from app.core.timeutils import utcnow

log = get_logger(__name__)


# --- State KV (z_m365ai_ingest_state) ---------------------------------------

def state_get(engine: Engine, key: str) -> Any | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT v FROM z_m365ai_ingest_state WHERE k=:k"),
            {"k": key},
        ).scalar_one_or_none()
    if row is None:
        return None
    return json.loads(row) if isinstance(row, str) else row


def state_set(engine: Engine, key: str, value: Any) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO z_m365ai_ingest_state (k, v) VALUES (:k, :v) "
                "ON DUPLICATE KEY UPDATE v=VALUES(v)"
            ),
            {"k": key, "v": json.dumps(value)},
        )


# --- Run lifecycle (z_audit_logs_efk_runs) ----------------------------------

def run_open(engine: Engine, source: str, report_date: date, *, manual: bool = False) -> int:
    """Open a run row, reusing today's running row for the same (source, manual)."""
    with engine.begin() as conn:
        existing = conn.execute(
            text(
                "SELECT id FROM z_audit_logs_efk_runs "
                "WHERE source=:s AND report_date=:d AND manual=:m AND status='running' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"s": source, "d": report_date, "m": 1 if manual else 0},
        ).first()
        if existing:
            return int(existing[0])
        res = conn.execute(
            text(
                "INSERT INTO z_audit_logs_efk_runs "
                "(source, report_date, started_at, status, manual, "
                " rows_in_csv, rows_inserted, rows_duplicate) "
                "VALUES (:s, :d, :t, 'running', :m, 0, 0, 0)"
            ),
            {"s": source, "d": report_date, "t": utcnow(), "m": 1 if manual else 0},
        )
        return int(res.lastrowid or 0)


def run_increment(
    engine: Engine,
    run_id: int,
    *,
    inserted: int = 0,
    duplicate: int = 0,
    scanned: int = 0,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE z_audit_logs_efk_runs SET "
                "rows_inserted  = COALESCE(rows_inserted,0)  + :i, "
                "rows_duplicate = COALESCE(rows_duplicate,0) + :d, "
                "rows_in_csv    = COALESCE(rows_in_csv,0)    + :s "
                "WHERE id=:r"
            ),
            {"i": inserted, "d": duplicate, "s": scanned, "r": run_id},
        )


def run_finalize(
    engine: Engine, run_id: int, status: str, error_excerpt: str | None = None
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE z_audit_logs_efk_runs SET finished_at=:t, status=:st, "
                "error_excerpt=:e WHERE id=:r"
            ),
            {
                "t": utcnow(),
                "st": status,
                "e": (error_excerpt[:2000] if error_excerpt else None),
                "r": run_id,
            },
        )


def reconcile_orphaned_runs(engine: Engine, max_age_seconds: int) -> int:
    """Move long-running rows that never advanced to status='error' (PLAN §11.4)."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE z_audit_logs_efk_runs SET status='error', finished_at=:t, "
                "error_excerpt='orphaned by restart' "
                "WHERE status='running' "
                "  AND TIMESTAMPDIFF(SECOND, started_at, :t) > :a "
                "  AND COALESCE(rows_inserted, 0) = 0"
            ),
            {"t": utcnow(), "a": max_age_seconds},
        )
        return int(result.rowcount or 0)


# --- Event insert (z_audit_logs_efk) ----------------------------------------

def insert_events(
    engine: Engine, rows: list[dict[str, Any]], run_id: int
) -> tuple[int, int]:
    """INSERT IGNORE bulk write. Returns (inserted, duplicate)."""
    if not rows:
        return (0, 0)
    inserted = 0
    duplicate = 0
    sql = text(
        "INSERT IGNORE INTO z_audit_logs_efk "
        "(timestamp, source, operation, instance, user_name, user_id, "
        " extra_data, comments, dedup_hash, ingest_run_id) "
        "VALUES (:timestamp, :source, :operation, :instance, :user_name, :user_id, "
        " :extra_data, :comments, :dedup_hash, :run_id)"
    )
    with engine.begin() as conn:
        for r in rows:
            params = dict(r)
            params["run_id"] = run_id
            if isinstance(params.get("extra_data"), (dict, list)):
                params["extra_data"] = json.dumps(
                    params["extra_data"], separators=(",", ":"), sort_keys=True, default=str
                )
            if params.get("comments") and len(str(params["comments"])) > 1024:
                params["comments"] = str(params["comments"])[:1024]
            result = conn.execute(sql, params)
            if result.rowcount == 1:
                inserted += 1
            else:
                duplicate += 1
    return inserted, duplicate


# --- Mapping rules (versioned, PLAN §3.6) -----------------------------------

def get_active_rules(engine: Engine, subsource: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT id, subsource, target_column, source_jsonpath, transform, "
                    "       version, valid_from, valid_to "
                    "FROM z_m365ai_mapping_rules "
                    "WHERE subsource=:s AND valid_to IS NULL"
                ),
                {"s": subsource},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def upsert_rule_version(
    engine: Engine,
    *,
    subsource: str,
    target_column: str,
    source_jsonpath: str,
    transform: str | None,
    actor: str,
) -> int:
    """Insert a new version. Never UPDATE the transform of an existing row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE z_m365ai_mapping_rules SET valid_to=:t "
                "WHERE subsource=:s AND target_column=:c AND source_jsonpath=:p "
                "  AND valid_to IS NULL"
            ),
            {"t": utcnow(), "s": subsource, "c": target_column, "p": source_jsonpath},
        )
        next_version = conn.execute(
            text(
                "SELECT COALESCE(MAX(version),0)+1 FROM z_m365ai_mapping_rules "
                "WHERE subsource=:s AND target_column=:c AND source_jsonpath=:p"
            ),
            {"s": subsource, "c": target_column, "p": source_jsonpath},
        ).scalar_one()
        res = conn.execute(
            text(
                "INSERT INTO z_m365ai_mapping_rules "
                "(subsource, target_column, source_jsonpath, transform, version, edited_by) "
                "VALUES (:s, :c, :p, :tr, :v, :e)"
            ),
            {
                "s": subsource,
                "c": target_column,
                "p": source_jsonpath,
                "tr": transform,
                "v": int(next_version),
                "e": actor,
            },
        )
        return int(res.lastrowid or 0)


def count_rules_for_subsource(engine: Engine, subsource: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM z_m365ai_mapping_rules WHERE subsource=:s"
                ),
                {"s": subsource},
            ).scalar_one()
        )


# --- App users --------------------------------------------------------------

def get_user(engine: Engine, username: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT username, password_hash, role, is_break_glass "
                    "FROM z_m365ai_app_users WHERE username=:u"
                ),
                {"u": username},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def upsert_user(
    engine: Engine,
    *,
    username: str,
    password_hash: str,
    role: str = "admin",
    is_break_glass: bool = True,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO z_m365ai_app_users "
                "(username, password_hash, role, is_break_glass) "
                "VALUES (:u, :h, :r, :b) "
                "ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash), "
                "  role=VALUES(role), is_break_glass=VALUES(is_break_glass)"
            ),
            {
                "u": username,
                "h": password_hash,
                "r": role,
                "b": 1 if is_break_glass else 0,
            },
        )


# --- Admin events -----------------------------------------------------------

def write_admin_event(
    engine: Engine,
    *,
    actor: str,
    actor_kind: str,
    action: str,
    target: str | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO z_m365ai_admin_events "
                "(actor, actor_kind, action, target, request_id, source_ip, details) "
                "VALUES (:a, :ak, :ac, :t, :rid, :ip, :d)"
            ),
            {
                "a": actor[:256],
                "ak": actor_kind[:16],
                "ac": action[:64],
                "t": target[:256] if target else None,
                "rid": request_id[:64] if request_id else None,
                "ip": source_ip[:64] if source_ip else None,
                "d": json.dumps(details, separators=(",", ":"), sort_keys=True, default=str)
                if details
                else None,
            },
        )


def list_admin_events(
    engine: Engine,
    *,
    limit: int = 200,
    action: str | None = None,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: dict[str, Any] = {"l": limit}
    if action:
        where.append("action=:ac")
        params["ac"] = action
    if actor:
        where.append("actor=:a")
        params["a"] = actor
    sql = (
        "SELECT id, ts, actor, actor_kind, action, target, request_id, source_ip, details "
        f"FROM z_m365ai_admin_events WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC, id DESC LIMIT :l"
    )
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


# --- Runs page query --------------------------------------------------------

def list_runs(
    engine: Engine,
    *,
    source: str | None = None,
    since: date | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: dict[str, Any] = {"l": limit}
    if source:
        where.append("source=:s")
        params["s"] = source
    if since:
        where.append("report_date>=:d")
        params["d"] = since
    sql = (
        "SELECT id, source, report_date, started_at, finished_at, status, "
        "       rows_in_csv, rows_inserted, rows_duplicate, manual, error_excerpt "
        f"FROM z_audit_logs_efk_runs WHERE {' AND '.join(where)} "
        "ORDER BY started_at DESC LIMIT :l"
    )
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]
