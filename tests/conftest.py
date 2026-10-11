"""Test fixtures: a throwaway database `b4p_test_<pid>` and the real data snapshots.

Set B4P_TEST_DATABASE_URL to point somewhere else (CI uses a postgres service
container). Tests never read .env and never touch b4p / b4p_v2.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# a per-process name so two people/agents running pytest at once don't drop each other's database
TEST_URL = os.environ.get("B4P_TEST_DATABASE_URL", f"postgresql:///b4p_test_{os.getpid()}")
# Make sure nothing in the code under test can fall back to a real database.
os.environ["DATABASE_URL"] = TEST_URL

from b4p import db  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
DATA_TABLES = ["shipment_pick", "sync_run", "shipment_item", "shipment", "requester", "inventory_item", "supply",
               "box", "box_type_alias", "box_type", "manufacturer_alias", "manufacturer",
               "category_alias", "category"]


@pytest.fixture(scope="session")
def test_db():
    name = conninfo_to_dict(TEST_URL)["dbname"]
    assert name.startswith("b4p_test"), f"refusing to use non-test database {name!r}"
    admin = make_conninfo(TEST_URL, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    with db.connect(TEST_URL) as conn:
        db.init_schema(conn)
        db.init_schema(conn)  # re-running init must be harmless
    yield TEST_URL
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def conn(test_db):
    """A connection to an empty, freshly seeded schema."""
    with db.connect(test_db) as c:
        c.execute(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE")
        c.commit()
        db.init_schema(c)  # re-seed lookup rows
        yield c


# ---------------------------------------------------------------------------
# real data snapshots
# ---------------------------------------------------------------------------
def load_sheet_csv(path=FIXTURES / "sheet_2026-10-09.csv") -> list[tuple[int, dict]]:
    from b4p.sources import parse_sheet_csv
    return parse_sheet_csv(Path(path).read_text())


def load_old_supplies() -> list[tuple[int, dict]]:
    from b4p.sources import parse_supplies_json
    with gzip.open(FIXTURES / "supplies_2026-09-06.json.gz", "rt") as f:
        return parse_supplies_json(json.load(f))


def load_requests(day="2026-10-09") -> list[dict]:
    return json.loads((FIXTURES / f"requests_{day}.json").read_text())


@pytest.fixture(scope="session")
def sheet_rows():
    return load_sheet_csv()


@pytest.fixture(scope="session")
def request_rows():
    return load_requests()


def write_csv(path, rows: list[tuple[int, dict]]):
    headers = list(rows[0][1])
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for _, r in rows:
            w.writerow([r.get(h, "") for h in headers])
