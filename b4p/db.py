"""Database connection helpers."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
DEFAULT_URL = "postgresql:///b4p_v2"


def database_url() -> str:
    """DATABASE_URL from the environment (the CLI loads .env first, without
    overriding variables that are already set)."""
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def connect(url: str | None = None, **kwargs) -> psycopg.Connection:
    # prepare_threshold=None: no server-side prepared statements, which the
    # Supabase/pgbouncer pooler does not support.
    return psycopg.connect(url or database_url(), prepare_threshold=None, **kwargs)


def init_schema(conn: psycopg.Connection) -> None:
    """Create/refresh all tables, views and functions (safe to re-run)."""
    conn.execute(SCHEMA.read_text())
    conn.commit()
