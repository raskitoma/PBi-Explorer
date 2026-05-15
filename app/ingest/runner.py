"""Worker entrypoint: schedules pollers, runs bootstrap pass on first run."""
from __future__ import annotations

import signal
from datetime import UTC, datetime, timedelta

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler

from app.core.config import reload_settings, settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import (
    cursor_lag_seconds,
    events_duplicate_total,
    events_ingested_total,
)
from app.db import repo
from app.db.bootstrap import fail_if_drift
from app.db.engine import get_engine
from app.ingest import graph as graph_ingest
from app.ingest import mgmt as mgmt_ingest
from app.ingest.dedup import compute_dedup_hash
from app.ingest.normalizer import (
    Rule,
    load_default_rules,
    normalize,
    rules_from_db_rows,
)

log = get_logger(__name__)


def _rules_for(subsource: str) -> list[Rule]:
    eng = get_engine()
    db_rows = repo.get_active_rules(eng, subsource)
    if db_rows:
        return rules_from_db_rows(db_rows)
    return load_default_rules(subsource)


def _process_events(
    subsource: str, events: list[dict], *, manual: bool = False
) -> tuple[int, int, int]:
    eng = get_engine()
    rules = _rules_for(subsource)
    today = datetime.now(UTC).date()
    run_id = repo.run_open(eng, "Microsoft365", today, manual=manual)
    rows: list[dict] = []
    latest_ts: datetime | None = None
    for ev in events:
        try:
            row = normalize(ev, subsource, rules)
        except Exception as e:  # noqa: BLE001
            log.warning("normalize.error", subsource=subsource, error=str(e))
            continue
        external_id = ev.get("id") or ev.get("Id") or ""
        row["dedup_hash"] = compute_dedup_hash(
            source="Microsoft365",
            subsource=subsource,
            external_id=str(external_id),
            ts=row["timestamp"],
        )
        rows.append(row)
        if latest_ts is None or row["timestamp"] > latest_ts:
            latest_ts = row["timestamp"]
    inserted, duplicate = repo.insert_events(eng, rows, run_id)
    repo.run_increment(eng, run_id, inserted=inserted, duplicate=duplicate, scanned=len(rows))
    if inserted:
        events_ingested_total.labels(subsource=subsource).inc(inserted)
    if duplicate:
        events_duplicate_total.labels(subsource=subsource).inc(duplicate)
    if latest_ts is not None:
        lag = (datetime.now(UTC) - latest_ts).total_seconds()
        cursor_lag_seconds.labels(feed=subsource).set(max(lag, 0.0))
    return inserted, duplicate, len(rows)


# --- Graph pollers ----------------------------------------------------------

def poll_graph_feed(feed: str) -> None:
    s = settings()
    eng = get_engine()
    state_key = f"graph:{feed}:next_link"
    cursor = repo.state_get(eng, state_key)
    url = cursor or graph_ingest.initial_url(feed, s.graph_lookback_hours)
    log.info("graph.poll.begin", feed=feed, has_cursor=bool(cursor))
    with httpx.Client() as client:
        try:
            for events, nxt in graph_ingest.walk(feed, url, client=client):
                ins, dup, scanned = _process_events(feed, events)
                log.info(
                    "graph.poll.page",
                    feed=feed,
                    scanned=scanned,
                    inserted=ins,
                    duplicate=dup,
                )
                if nxt:
                    repo.state_set(eng, state_key, nxt)
                else:
                    repo.state_set(eng, state_key, None)
                    break
        except Exception as e:  # noqa: BLE001
            log.error("graph.poll.error", feed=feed, error=str(e))


# --- Mgmt pollers -----------------------------------------------------------

def _subsource_for_content_type(ct: str) -> str:
    # 'Audit.SharePoint' -> 'mgmt.SharePoint'
    return f"mgmt.{ct.split('.', 1)[1]}" if "." in ct else f"mgmt.{ct}"


def poll_mgmt_content_type(ct: str) -> None:
    eng = get_engine()
    state_key = f"mgmt:{ct}:next_uri"
    start = repo.state_get(eng, state_key)
    log.info("mgmt.poll.begin", content_type=ct, has_cursor=bool(start))
    subsource = _subsource_for_content_type(ct)
    with httpx.Client() as client:
        sub_state = mgmt_ingest.ensure_subscriptions([ct], client=client)
        if not sub_state.get(ct):
            log.warning("mgmt.poll.subscription_unavailable", content_type=ct)
            return
        try:
            for descriptors, nxt in mgmt_ingest.list_content(
                ct, client=client, start_uri=start
            ):
                for d in descriptors:
                    blob = mgmt_ingest.fetch_blob(d["contentUri"], client=client)
                    _process_events(subsource, blob)
                if nxt:
                    repo.state_set(eng, state_key, nxt)
                else:
                    repo.state_set(eng, state_key, None)
                    break
        except Exception as e:  # noqa: BLE001
            log.error("mgmt.poll.error", content_type=ct, error=str(e))


# --- Worker entrypoint ------------------------------------------------------

def _reload_handler(signum: int, _frame: object) -> None:
    log.info("worker.reload", signum=signum)
    reload_settings()


def main() -> None:
    configure_logging()
    s = settings()
    log.info("worker.starting", version="0.1.0", cloud=s.azure_cloud)
    engine = get_engine()
    fail_if_drift(engine)
    n = repo.reconcile_orphaned_runs(engine, max_age_seconds=4 * s.poll_interval_s)
    if n:
        log.info("worker.orphan.reconciled", count=n)

    sched = BlockingScheduler(timezone="UTC")
    now = datetime.now(UTC)
    for feed in ("graph.directoryAudits", "graph.signIns", "graph.provisioning"):
        sched.add_job(
            poll_graph_feed,
            "interval",
            args=[feed],
            seconds=s.poll_interval_s,
            id=f"poll-{feed}",
            next_run_time=now + timedelta(seconds=1),
            max_instances=1,
            coalesce=True,
        )
    for ct in s.mgmt_content_type_list:
        sched.add_job(
            poll_mgmt_content_type,
            "interval",
            args=[ct],
            seconds=s.poll_interval_s,
            id=f"poll-mgmt-{ct}",
            next_run_time=now + timedelta(seconds=2),
            max_instances=1,
            coalesce=True,
        )

    signal.signal(signal.SIGHUP, _reload_handler)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
