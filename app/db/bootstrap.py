"""Schema bootstrap: enforce z_audit_logs_efk* contract, create aux tables.

Karpathy: surgical — never ALTER the contract tables; verify them, refuse on drift.
"""
from __future__ import annotations

import sys
from textwrap import dedent

from sqlalchemy import Engine, text

from app.core.logging import get_logger

log = get_logger(__name__)

# ---- Verbatim contract DDL (PLAN §3.1 / §7) --------------------------------

DDL_AUDIT_LOGS = dedent(
    """
    CREATE TABLE IF NOT EXISTS z_audit_logs_efk (
      id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      timestamp       DATETIME(3)     NOT NULL,
      source          VARCHAR(64)     NOT NULL,
      operation       VARCHAR(128)    NOT NULL,
      instance        VARCHAR(128)    NOT NULL,
      user_name       VARCHAR(256)    DEFAULT NULL,
      user_id         VARCHAR(64)     DEFAULT NULL,
      extra_data      JSON            DEFAULT NULL,
      comments        VARCHAR(1024)   DEFAULT NULL,
      dedup_hash      CHAR(64)        NOT NULL,
      ingested_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
      ingest_run_id   BIGINT UNSIGNED DEFAULT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_dedup_hash (dedup_hash),
      KEY ix_source_ts (source, timestamp),
      KEY ix_ts        (timestamp),
      KEY ix_userid_ts (user_id, timestamp)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
).strip()

DDL_AUDIT_RUNS = dedent(
    """
    CREATE TABLE IF NOT EXISTS z_audit_logs_efk_runs (
      id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      source          VARCHAR(64)     NOT NULL,
      report_date     DATE            NOT NULL,
      started_at      DATETIME(3)     NOT NULL,
      finished_at     DATETIME(3)     DEFAULT NULL,
      status          VARCHAR(32)     NOT NULL,
      rows_in_csv     INT UNSIGNED    DEFAULT NULL,
      rows_inserted   INT UNSIGNED    DEFAULT NULL,
      rows_duplicate  INT UNSIGNED    DEFAULT NULL,
      manual          TINYINT(1)      NOT NULL DEFAULT 0,
      error_excerpt   VARCHAR(2000)   DEFAULT NULL,
      screenshot_path VARCHAR(512)    DEFAULT NULL,
      ok_scheduled_date DATE GENERATED ALWAYS AS
        (CASE WHEN status='ok' AND manual=0 THEN report_date ELSE NULL END) VIRTUAL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_ok_scheduled_date (source, ok_scheduled_date),
      KEY ix_source_date (source, report_date),
      KEY ix_started (started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
).strip()

# ---- Aux tables (PLAN §3.3) ------------------------------------------------

DDL_AUX = [
    dedent(
        """
        CREATE TABLE IF NOT EXISTS z_m365ai_ingest_state (
          k VARCHAR(128) PRIMARY KEY,
          v JSON NOT NULL,
          updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                            ON UPDATE CURRENT_TIMESTAMP(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    ).strip(),
    dedent(
        """
        CREATE TABLE IF NOT EXISTS z_m365ai_mapping_rules (
          id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
          subsource       VARCHAR(64)  NOT NULL,
          target_column   VARCHAR(64)  NOT NULL,
          source_jsonpath VARCHAR(512) NOT NULL,
          transform       VARCHAR(256) DEFAULT NULL,
          version         INT UNSIGNED NOT NULL DEFAULT 1,
          valid_from      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
          valid_to        DATETIME(3)  DEFAULT NULL,
          edited_by       VARCHAR(128) DEFAULT NULL,
          UNIQUE KEY uk_rule_version (subsource, target_column, source_jsonpath, version),
          KEY ix_active (subsource, target_column, valid_to)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    ).strip(),
    dedent(
        """
        CREATE TABLE IF NOT EXISTS z_m365ai_app_users (
          username       VARCHAR(64) PRIMARY KEY,
          password_hash  VARCHAR(255) NOT NULL,
          role           VARCHAR(16)  NOT NULL DEFAULT 'admin',
          is_break_glass TINYINT(1)   NOT NULL DEFAULT 0,
          created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    ).strip(),
    dedent(
        """
        CREATE TABLE IF NOT EXISTS z_m365ai_admin_events (
          id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
          ts          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
          actor       VARCHAR(256) NOT NULL,
          actor_kind  VARCHAR(16)  NOT NULL,
          action      VARCHAR(64)  NOT NULL,
          target      VARCHAR(256) DEFAULT NULL,
          request_id  VARCHAR(64)  DEFAULT NULL,
          source_ip   VARCHAR(64)  DEFAULT NULL,
          details     JSON         DEFAULT NULL,
          KEY ix_admin_ts (ts),
          KEY ix_admin_actor (actor),
          KEY ix_admin_action (action, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    ).strip(),
]

# ---- Expected shapes for drift detection -----------------------------------

EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "z_audit_logs_efk": {
        "id": "bigint",
        "timestamp": "datetime(3)",
        "source": "varchar(64)",
        "operation": "varchar(128)",
        "instance": "varchar(128)",
        "user_name": "varchar(256)",
        "user_id": "varchar(64)",
        "extra_data": "json",
        "comments": "varchar(1024)",
        "dedup_hash": "char(64)",
        "ingested_at": "datetime(3)",
        "ingest_run_id": "bigint",
    },
    "z_audit_logs_efk_runs": {
        "id": "bigint",
        "source": "varchar(64)",
        "report_date": "date",
        "started_at": "datetime(3)",
        "finished_at": "datetime(3)",
        "status": "varchar(32)",
        "rows_in_csv": "int",
        "rows_inserted": "int",
        "rows_duplicate": "int",
        "manual": "tinyint(1)",
        "error_excerpt": "varchar(2000)",
        "screenshot_path": "varchar(512)",
        "ok_scheduled_date": "date",
    },
}


class SchemaDriftError(RuntimeError):
    pass


def _column_type_matches(expected: str, actual: str) -> bool:
    """Compare an expected MySQL/MariaDB column type to what
    INFORMATION_SCHEMA.COLUMNS.COLUMN_TYPE reports.

    MariaDB implements `JSON` as `LONGTEXT` with an implicit JSON_VALID
    CHECK constraint and reports it as `longtext` in INFORMATION_SCHEMA.
    MySQL keeps a distinct `json` type. Accept both so the same drift
    detector works against either backend without false positives.
    """
    actual = actual.lower()
    expected = expected.lower()
    if expected == "json":
        return actual in {"json", "longtext"}
    return actual.startswith(expected)


def _existing_columns(engine: Engine, table: str) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COLUMN_NAME, COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"
            ),
            {"t": table},
        ).all()
    return {str(r[0]): str(r[1]).lower() for r in rows}


def _table_exists(engine: Engine, table: str) -> bool:
    with engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"
            ),
            {"t": table},
        ).scalar_one()
    return int(n) == 1


def verify_contract(engine: Engine) -> None:
    """Either the contract tables don't exist (we'll create them) or they match exactly."""
    diffs: list[str] = []
    for table, expected in EXPECTED_COLUMNS.items():
        if not _table_exists(engine, table):
            continue
        got = _existing_columns(engine, table)
        for col, exp_type in expected.items():
            if col not in got:
                diffs.append(f"{table}: expected column {col} ({exp_type}), found <missing>")
                continue
            if not _column_type_matches(exp_type, got[col]):
                diffs.append(f"{table}: expected column {col} {exp_type}, found {got[col]}")
        extra = set(got) - set(expected)
        if extra:
            diffs.append(f"{table}: unexpected columns {sorted(extra)}")
    if diffs:
        raise SchemaDriftError("schema mismatch: " + " | ".join(diffs))


def run_bootstrap(engine: Engine) -> None:
    verify_contract(engine)
    with engine.begin() as conn:
        conn.execute(text(DDL_AUDIT_LOGS))
        conn.execute(text(DDL_AUDIT_RUNS))
        for ddl in DDL_AUX:
            conn.execute(text(ddl))
    log.info("db.bootstrap.ok")


def fail_if_drift(engine: Engine) -> None:
    """Public entrypoint used at web + worker startup."""
    try:
        run_bootstrap(engine)
    except SchemaDriftError as e:
        log.error("db.bootstrap.drift", error=str(e))
        print(str(e), file=sys.stderr)
        sys.exit(78)  # EX_CONFIG


AUX_TABLES = (
    "z_m365ai_ingest_state",
    "z_m365ai_mapping_rules",
    "z_m365ai_app_users",
    "z_m365ai_admin_events",
)


def check_schema_status(engine: Engine) -> dict[str, object]:
    """Non-raising introspection for the GUI status panel.

    Returns:
        {
          "db_connected": bool,
          "db_error":     str | None,
          "tables_present": {table: bool, ...},
          "drift":        {table: [diff, ...]},     # only present when drift exists
          "ready":        bool,
        }
    """
    out: dict[str, object] = {
        "db_connected": False,
        "db_error": None,
        "tables_present": {},
        "drift": {},
        "ready": False,
    }
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        out["db_connected"] = True
    except Exception as e:  # noqa: BLE001
        out["db_error"] = str(e)[:500]
        return out

    all_tables = list(EXPECTED_COLUMNS.keys()) + list(AUX_TABLES)
    present: dict[str, bool] = {}
    for t in all_tables:
        present[t] = _table_exists(engine, t)
    out["tables_present"] = present

    drift: dict[str, list[str]] = {}
    for table, expected in EXPECTED_COLUMNS.items():
        if not present.get(table):
            continue
        got = _existing_columns(engine, table)
        diffs: list[str] = []
        for col, exp_type in expected.items():
            if col not in got:
                diffs.append(f"missing column {col} ({exp_type})")
                continue
            if not _column_type_matches(exp_type, got[col]):
                diffs.append(f"{col}: expected {exp_type}, found {got[col]}")
        if diffs:
            drift[table] = diffs
    if drift:
        out["drift"] = drift

    out["ready"] = all(present.values()) and not drift
    return out
