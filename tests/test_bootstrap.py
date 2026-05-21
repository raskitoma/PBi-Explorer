"""Schema bootstrap tests. Marked integration — require a real MariaDB.

Set MARIADB_TEST_DSN to run, e.g. `mysql+pymysql://root:root@127.0.0.1:3306/m365_test`.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.db.bootstrap import SchemaDriftError, run_bootstrap, verify_contract

DSN = os.environ.get("MARIADB_TEST_DSN")

pytestmark = pytest.mark.integration


def _engine():
    if not DSN:
        pytest.skip("MARIADB_TEST_DSN not set")
    return create_engine(DSN, future=True, pool_pre_ping=True)


def _drop_all(engine) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        for t in (
            "z_audit_logs_efk",
            "z_audit_logs_efk_runs",
            "z_m365ai_ingest_state",
            "z_m365ai_mapping_rules",
            "z_m365ai_app_users",
            "z_m365ai_admin_events",
        ):
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))


def test_bootstrap_creates_then_is_idempotent() -> None:
    engine = _engine()
    _drop_all(engine)
    run_bootstrap(engine)
    # Second call must be a no-op (does not raise).
    run_bootstrap(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME IN ('z_audit_logs_efk','z_audit_logs_efk_runs',"
                "'z_m365ai_ingest_state','z_m365ai_mapping_rules',"
                "'z_m365ai_app_users','z_m365ai_admin_events')"
            )
        ).scalar_one()
    assert row == 6


def test_drift_detection() -> None:
    engine = _engine()
    _drop_all(engine)
    # Create z_audit_logs_efk WITHOUT dedup_hash → must trigger drift.
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE z_audit_logs_efk ("
                " id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                " timestamp DATETIME(3) NOT NULL,"
                " source VARCHAR(64) NOT NULL,"
                " operation VARCHAR(128) NOT NULL,"
                " instance VARCHAR(128) NOT NULL,"
                " user_name VARCHAR(256) NULL,"
                " user_id VARCHAR(64) NULL,"
                " extra_data JSON NULL,"
                " comments VARCHAR(1024) NULL,"
                " ingested_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),"
                " ingest_run_id BIGINT UNSIGNED NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
    with pytest.raises(SchemaDriftError) as exc:
        verify_contract(engine)
    assert "dedup_hash" in str(exc.value)
    _drop_all(engine)
