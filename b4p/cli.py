"""Command line:  python -m b4p init | sync [--loop [SECONDS]] | check | status | doctor"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import time

import psycopg

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
    if args.aliases:
        return _alias_report()
    with db.connect() as conn:
        results = checks.run_checks(conn)
        for label, n in results:
            print(f"{'WARN' if n else 'ok  '}  {label}: {n}")
            if n and args.details:
                for line in checks.details(conn, label, args.limit):
                    print(f"        {line}")
                if n > args.limit and checks.DETAILS.get(label):
                    print(f"        ... and {n - args.limit} more")
    return 0


def cmd_doctor(args) -> int:
    from b4p import doctor
    results = doctor.run_doctor()
    for level, topic, msg in results:
        print(f"{level.upper():4}  {topic:11} {msg}")
    return 1 if any(level == "fail" for level, _, _ in results) else 0


def _alias_report() -> int:
    from b4p import aliasgap
    with db.connect() as conn:
        gaps = aliasgap.alias_gaps(conn)
    for kind, items in gaps.items():
        print(f"{kind}: {len(items)} spelling(s) not mapped")
        for spelling, n, canon in items[:25]:
            human = kind == "manufacturer" and aliasgap.needs_a_human(spelling)
            print(f"  {spelling!r} ({n} rows)" + (f" -> maybe {canon!r}" if canon else "")
                  + ("   [needs a human: may be two companies]" if human else ""))
    sql = aliasgap.suggestions(gaps)
    if sql:
        print("\n-- suggested alias rows: GUESSES from similar spellings (a \"X / Y\" co-brand may be a different maker), review before running in psql:")
        print("\n".join(sql))
    return 0


def cmd_status(args) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT total_entries, total_raw_items, box_count, awaiting_review FROM v_kpis")
        e, units, boxes, review = cur.fetchone()
        cur.execute("SELECT count(*) FROM inventory_item WHERE removed_at IS NOT NULL")
        removed = cur.fetchone()[0]
        cur.execute("SELECT count(*), count(*) FILTER (WHERE source = 'app') FROM shipment")
        reqs, app_reqs = cur.fetchone()
        cur.execute("SELECT status, count(DISTINCT request_id) FROM (SELECT DISTINCT \"Request ID\" AS request_id, "
                    "\"Status\" AS status FROM dash_requests) x GROUP BY status ORDER BY 2 DESC")
        by_status = ", ".join(f"{n} {s.lower()}" for s, n in cur.fetchall())
        print(f"inventory: {e} active rows, {units} units, {boxes} boxes, "
              f"{review} awaiting review, {removed} removed")
        print(f"requests:  {reqs} ({app_reqs} via dashboard)" + (f" -- {by_status}" if by_status else ""))
        cur.execute("SELECT last_success_at, last_run_at, last_run_ok, last_warning_count FROM dash_sync_status")
        ok_at, run_at, run_ok, warns = cur.fetchone()
        if run_at is None:
            print("last sync: never")
        else:
            print(f"last sync: {'ok' if run_ok else 'FAILED'} at {run_at:%Y-%m-%d %H:%M}"
                  + (f", {warns} warning(s)" if warns else ""))
            if not run_ok:
                cur.execute("SELECT run_id, COALESCE(error, 'refused: ' || refused), source FROM sync_run ORDER BY run_id DESC LIMIT 1")
                rid, err, src = cur.fetchone()
                print(f"  run #{rid} [{src}]: " + (err or "no error recorded").splitlines()[0][:160])
                print("  last good sync: " + (f"{ok_at:%Y-%m-%d %H:%M}" if ok_at else "never"))
        nwarn = sum(1 for _, n in checks.run_checks(conn) if n)
        print(f"checks:    {nwarn} warning(s) -- run `check --details` to see which rows")
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
    pc = sub.add_parser("check", help="run consistency checks")
    pc.add_argument("--details", action="store_true", help="list the rows behind each warning")
    pc.add_argument("--limit", type=int, default=10, help="rows per warning with --details")
    pc.add_argument("--aliases", action="store_true", help="list spellings no alias maps yet, with suggested rows")
    pc.set_defaults(fn=cmd_check)
    sub.add_parser("doctor", help="check setup (database target, views, credentials); read-only").set_defaults(fn=cmd_doctor)
    sub.add_parser("status", help="counts and last sync").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedFunction) as e:
        if args.cmd == "init":
            raise
        print(f"The database schema is old or missing ({str(e).strip().splitlines()[0][:100]}). "
              "Run `python -m b4p init` first.")
        return 2
