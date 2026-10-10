"""Live smoke test against the real feeds. Run with:  pytest -m live"""
import pytest

from b4p import sources
from b4p.sync import run_sync
from conftest import load_requests

pytestmark = pytest.mark.live


def test_live_sync(conn):
    """Needs a service-account key (the public CSV export is currently not shared)."""
    try:
        source, inventory = sources.fetch_inventory()
    except sources.FeedError as e:
        pytest.skip(f"no inventory source reachable: {e}")
    requests = load_requests()
    assert len(inventory) > 500
    s = run_sync(conn, inventory, requests, source=source)
    assert s["items_added"] > 500 and not s["errors"]
    again = run_sync(conn, inventory, requests, source=source)
    assert (again["items_added"], again["items_updated"], again["items_removed"]) == (0, 0, 0)
    n = conn.execute("SELECT count(*) FROM dash_items").fetchone()[0]
    assert n == s["items_added"]
    assert conn.execute("SELECT count(*) FROM dash_requests").fetchone()[0] == len(requests)


def test_live_service_account_reads_the_sheet():
    import os
    if not (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")):
        pytest.skip("no service-account key configured")
    src, rows = sources.fetch_inventory()
    assert src.startswith("service-account:") and len(rows) > 500
