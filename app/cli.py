"""Manual CLI: backfill, mgmt-subs."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from app.core.logging import configure_logging, get_logger
from app.core.timeutils import parse_iso
from app.db import repo
from app.db.engine import get_engine
from app.ingest import graph as graph_ingest
from app.ingest import mgmt as mgmt_ingest
from app.ingest.runner import _process_events

log = get_logger(__name__)


def cmd_backfill(args: argparse.Namespace) -> int:
    since = parse_iso(args.since)
    until = parse_iso(args.until) if args.until else datetime.now(UTC)
    eng = get_engine()
    for ss in args.subsource:
        run_id = repo.run_open(eng, "Microsoft365", since.date(), manual=True)
        log.info("backfill.start", subsource=ss, since=since.isoformat(), until=until.isoformat(), run_id=run_id)
        try:
            if ss.startswith("graph."):
                hours = int((datetime.now(UTC) - since).total_seconds() // 3600) + 1
                url = graph_ingest.initial_url(ss, hours)
                with httpx.Client() as client:
                    for events, nxt in graph_ingest.walk(ss, url, client=client):
                        windowed = [
                            e for e in events
                            if since <= parse_iso(e.get("activityDateTime") or e.get("createdDateTime") or until.isoformat()) < until
                        ]
                        _process_events(ss, windowed, manual=True)
                        if not nxt:
                            break
            elif ss.startswith("mgmt."):
                ct = "Audit." + ss.split(".", 1)[1]
                with httpx.Client() as client:
                    for descriptors, nxt in mgmt_ingest.list_content(ct, client=client):
                        for d in descriptors:
                            blob = mgmt_ingest.fetch_blob(d["contentUri"], client=client)
                            _process_events(ss, blob, manual=True)
                        if not nxt:
                            break
            else:
                log.error("backfill.unknown_subsource", subsource=ss)
                repo.run_finalize(eng, run_id, "error", error_excerpt=f"unknown subsource: {ss}")
                continue
            repo.run_finalize(eng, run_id, "ok")
        except Exception as e:  # noqa: BLE001
            log.error("backfill.error", subsource=ss, error=str(e))
            repo.run_finalize(eng, run_id, "error", error_excerpt=str(e))
    return 0


def cmd_mgmt_subs(args: argparse.Namespace) -> int:
    with httpx.Client() as client:
        state = mgmt_ingest.ensure_subscriptions(args.start, client=client)
    log.info("mgmt.subs.state", state=state)
    return 0 if all(state.values()) else 1


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="m365ai")
    sub = p.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", help="Re-walk an explicit time window")
    bf.add_argument("--subsource", required=True, nargs="+",
                    help="e.g. graph.directoryAudits mgmt.SharePoint")
    bf.add_argument("--since", required=True, help="ISO-8601 UTC")
    bf.add_argument("--until", help="ISO-8601 UTC (default: now)")
    bf.set_defaults(func=cmd_backfill)

    ms = sub.add_parser("mgmt-subs", help="Start/verify Mgmt API subscriptions")
    ms.add_argument("--start", required=True, nargs="+", help="content types")
    ms.set_defaults(func=cmd_mgmt_subs)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
