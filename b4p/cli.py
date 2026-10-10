"""Command line:  python -m b4p init | sync [--loop [SECONDS]] | check | status"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import time

from b4p import checks, db, sync


def cmd_init(args) -> int:
    with db.connect() as conn:
        db.init_schema(conn)
    print("schema applied")
    return 0


def _sync_once() -> int:
    with db.connect() as conn:
        stats = sync.run_sync(conn)
        print(sync.summary(stats), flush=True)
        for label, n in checks.run_checks(conn):
            if n:
                print(f"  check: {label}: {n}", flush=True)
    return 1 if stats["errors"] else 0


def cmd_sync(args) -> int:
    if args.loop is None:
        return _sync_once()
    while True:  # replaces the old monitor.py
        try:
            _sync_once()
        except Exception as e:  # keep looping through network/db hiccups
            logging.error("sync failed: %s", e)
        time.sleep(args.loop)


def cmd_check(args) -> int:
    with db.connect() as conn:
        results = checks.run_checks(conn)
    for label, n in results:
        print(f"{'WARN' if n else 'ok  '}  {label}: {n}")
    return 0


def cmd_status(args) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT total_entries, total_raw_items, box_count, awaiting_review FROM v_kpis")
        e, units, boxes, review = cur.fetchone()
        cur.execute("SELECT count(*) FROM inventory_item WHERE removed_at IS NOT NULL")
        removed = cur.fetchone()[0]
        cur.execute("SELECT count(*), count(*) FILTER (WHERE source = 'app') FROM shipment")
        reqs, app_reqs = cur.fetchone()
        print(f"inventory: {e} active rows, {units} units, {boxes} boxes, "
              f"{review} awaiting review, {removed} removed")
        print(f"requests:  {reqs} ({app_reqs} created via dashboard)")
        cur.execute("SELECT run_id, finished_at, source, items_added, items_updated, items_removed, error "
                    "FROM sync_run ORDER BY run_id DESC LIMIT 1")
        last = cur.fetchone()
        if last:
            print(f"last sync: #{last[0]} at {last[1]:%Y-%m-%d %H:%M} [{last[2]}] "
                  f"+{last[3]} ~{last[4]} -{last[5]}" + (f" ERROR {last[6]}" if last[6] else ""))
        else:
            print("last sync: never")
    return 0


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv
        # Only ./.env (no search up parent folders), and a real env var wins over it.
        load_dotenv(Path.cwd() / ".env", override=False)
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m b4p", description="B4P inventory database")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create/update tables, views and functions").set_defaults(fn=cmd_init)
    s = sub.add_parser("sync", help="pull the sheet + requests feed into the database")
    s.add_argument("--loop", nargs="?", type=int, const=300, metavar="SECONDS",
                   help="keep syncing every SECONDS (default 300)")
    s.set_defaults(fn=cmd_sync)
    sub.add_parser("check", help="run consistency checks").set_defaults(fn=cmd_check)
    sub.add_parser("status", help="counts and last sync").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    return args.fn(args)
