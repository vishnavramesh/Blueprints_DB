"""`b4p doctor`: is this machine set up to run b4p? Read-only; never prints secrets.

Reports which database it is pointed at (host and name only, never the password),
whether it can connect, whether the dashboard views/functions exist, and whether
sheet credentials are present.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from b4p import db

VIEWS = ["dash_items", "dash_supplies", "dash_requests", "dash_sync_status", "v_inventory", "v_kpis",
         "v_expiring", "v_review_queue"]
TABLES = ["category", "manufacturer", "box", "supply", "inventory_item", "shipment", "shipment_item",
          "requester", "sync_run", "category_alias", "manufacturer_alias", "box_type_alias"]
FUNCTIONS = ["create_request", "set_request_status", "set_item_quantity", "redact_phi", "b4p_has_phi"]


def describe_target(url: str) -> tuple[str, bool]:
    """('host:port/dbname', is_local) without the user or password."""
    try:
        d = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return "(unparseable DATABASE_URL)", False
    host = d.get("host") or ""
    local = host == "" or host.startswith("/") or host in ("localhost", "127.0.0.1", "::1")
    where = "local socket" if host == "" or host.startswith("/") else f"{host}:{d.get('port', '5432')}"
    return f"{where}/{d.get('dbname', '?')}", local


def run_doctor() -> list[tuple[str, str, str]]:
    """[(level, topic, message)] with level 'ok' / 'warn' / 'fail'."""
    out: list[tuple[str, str, str]] = []
    add = lambda lvl, topic, msg: out.append((lvl, topic, msg))  # noqa: E731

    url = db.database_url()
    if not os.environ.get("DATABASE_URL"):
        add("warn", "env", "DATABASE_URL is not set; using the built-in default")
    target, local = describe_target(url)
    add("ok" if local else "warn", "database", f"pointed at {target}" + ("" if local else " (REMOTE: not a local database)"))

    try:
        # read-only session: doctor can never change anything
        conn = db.connect(connect_timeout=8, options="-c default_transaction_read_only=on")
    except psycopg.Error as e:
        add("fail", "connect", f"cannot connect ({type(e).__name__}: {str(e).strip().splitlines()[0][:120]})")
        conn = None
    if conn is not None:
        with conn:
            ver = conn.execute("SHOW server_version").fetchone()[0]
            add("ok", "connect", f"connected, PostgreSQL {ver}")
            have_tables = {r[0] for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
            missing = [t for t in TABLES if t not in have_tables]
            add("fail" if missing else "ok", "tables",
                f"missing: {', '.join(missing)} (run `b4p init`)" if missing else f"all {len(TABLES)} tables exist")
            have_views = {r[0] for r in conn.execute(
                "SELECT viewname FROM pg_views WHERE schemaname = current_schema()")}
            missing = [v for v in VIEWS if v not in have_views]
            add("fail" if missing else "ok", "views",
                f"missing: {', '.join(missing)} (run `b4p init`)" if missing else f"all {len(VIEWS)} dashboard views exist")
            have_fns = {r[0] for r in conn.execute(
                "SELECT proname FROM pg_proc WHERE pronamespace = current_schema()::regnamespace")}
            missing = [f for f in FUNCTIONS if f not in have_fns]
            add("fail" if missing else "ok", "functions",
                f"missing: {', '.join(missing)} (run `b4p init`)" if missing else f"all {len(FUNCTIONS)} contract functions exist")
            if "dash_sync_status" in have_views:
                last_ok, ran, ok, n = conn.execute(
                    "SELECT last_success_at, last_run_at, last_run_ok, active_items FROM dash_sync_status").fetchone()
                if ran is None:
                    add("warn", "sync", "no sync has run yet")
                else:
                    add("ok" if ok else "warn", "sync",
                        f"last run {ran:%Y-%m-%d %H:%M} {'ok' if ok else 'did not refresh the data'}; "
                        f"last good sync {last_ok:%Y-%m-%d %H:%M}" if last_ok else
                        f"last run {ran:%Y-%m-%d %H:%M} did not refresh the data; no good sync yet")
                    add("ok", "inventory", f"{n} active items")

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if sa_json:
        add("ok", "credentials", "service-account key: set via GOOGLE_SERVICE_ACCOUNT_JSON (value not shown)")
    elif sa_path:
        add("ok" if Path(sa_path).is_file() else "fail", "credentials",
            "service-account key file " + ("found" if Path(sa_path).is_file() else "NOT FOUND") + " (GOOGLE_APPLICATION_CREDENTIALS)")
    else:
        add("warn", "credentials", "no service-account key; sync falls back to the public CSV link, which may be refused")
    if (Path.cwd() / ".env").exists():
        add("ok", "env", ".env found in this folder (contents not shown)")
    return out
