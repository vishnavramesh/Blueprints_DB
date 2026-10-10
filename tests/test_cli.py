"""CLI commands and configuration."""
import os

import pytest

from b4p import cli, db, sources
from conftest import TEST_URL, load_requests, load_sheet_csv


@pytest.fixture
def offline(monkeypatch):
    """Serve the fixture snapshots instead of the network."""
    monkeypatch.setattr(sources, "fetch_inventory", lambda: ("fixture", load_sheet_csv()))
    monkeypatch.setattr(sources, "fetch_requests", lambda: ("fixture", load_requests()))


def test_env_var_beats_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://should-not-be-used/x\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    from dotenv import load_dotenv
    load_dotenv(tmp_path / ".env", override=False)
    assert db.database_url() == TEST_URL


def test_dotenv_used_when_env_unset(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql:///from_dotenv\n")
    monkeypatch.delenv("DATABASE_URL")
    from dotenv import load_dotenv
    load_dotenv(tmp_path / ".env", override=False)
    assert db.database_url() == "postgresql:///from_dotenv"
    monkeypatch.setenv("DATABASE_URL", TEST_URL)


def test_cli_init_sync_check_status(conn, offline, capsys):
    assert os.environ["DATABASE_URL"] == TEST_URL
    assert cli.main(["init"]) == 0
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "+1136 added" in out
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "+0 added, 0 updated, 0 removed" in out and "requests +0, lines +0" in out
    assert cli.main(["check"]) == 0
    assert "flagged rows awaiting review" in capsys.readouterr().out
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "1136 active rows" in out and "requests:  12" in out


def test_cli_sync_reports_feed_failure(conn, monkeypatch, capsys):
    def boom():
        raise sources.FeedError("offline")
    monkeypatch.setattr(sources, "fetch_inventory", boom)
    monkeypatch.setattr(sources, "fetch_requests", boom)
    assert cli.main(["sync"]) == 1
    assert "ERRORS" in capsys.readouterr().out
    with db.connect(TEST_URL) as c:  # the failed run is still recorded
        err = c.execute("SELECT error FROM sync_run ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    assert "inventory feed" in err


def test_sheet_csv_parser_rejects_login_page():
    with pytest.raises(sources.FeedError):
        sources.parse_sheet_csv("<!DOCTYPE html><html>Sign in</html>")


def test_sheet_csv_parser_rows_and_blank_headers():
    rows = sources.parse_sheet_csv("Name,,Quantity\nGauze,x,3\n,,\nTape,,1\n")
    assert rows == [(2, {"Name": "Gauze", "Quantity": "3"}), (3, {"Name": "", "Quantity": ""}),
                    (4, {"Name": "Tape", "Quantity": "1"})]


def test_supplies_json_parser_keeps_sheet_row():
    rows = sources.parse_supplies_json([{"Name": "A", "Unnamed_13": "", "_sheet_row": 5}])
    assert rows == [(5, {"Name": "A"})]


def test_values_to_rows_pads_and_drops_blank_headers():
    grid = [["Name", "", "Quantity", "Name"], ["Gauze", "x"], ["Tape", "", "2", "dup"]]
    assert sources.values_to_rows(grid) == [(2, {"Name": "Gauze", "Quantity": ""}),
                                            (3, {"Name": "Tape", "Quantity": "2"})]


def test_inventory_source_order(monkeypatch):
    """service account -> public CSV; there is NO fallback to the dashboards' own feed."""
    calls = []

    class FakeWS:
        title = "Final Inventorying"

        def get_all_values(self):
            return [["Name", "Quantity"], ["Gauze\x00", "3"]]

    class FakeBook:
        def get_worksheet_by_id(self, gid):
            calls.append(("gid", gid))
            return FakeWS()

    class FakeClient:
        def open_by_key(self, key):
            return FakeBook()

    monkeypatch.setattr(sources, "_gspread_client", lambda: FakeClient())
    src, rows = sources.fetch_inventory()
    assert src == "service-account:Final Inventorying" and rows == [(2, {"Name": "Gauze", "Quantity": "3"})]
    assert calls == [("gid", int(sources.DEFAULT_TAB_GID))]

    # no key configured -> public CSV
    monkeypatch.setattr(sources, "_gspread_client", lambda: None)

    class R:
        text = "Name,Quantity\nTape,1\n"
    monkeypatch.setattr(sources, "_get", lambda url: calls.append(url) or R())
    src, rows = sources.fetch_inventory()
    assert src.startswith("sheet-csv") and rows == [(2, {"Name": "Tape", "Quantity": "1"})]
    assert "docs.google.com" in calls[-1]

    # CSV not public -> the run fails; nothing else (e.g. Render) is tried
    def deny(url):
        calls.append(url)
        raise sources.FeedError("401")
    monkeypatch.setattr(sources, "_get", deny)
    n = len(calls)
    with pytest.raises(sources.FeedError):
        sources.fetch_inventory()
    assert len(calls) == n + 1 and "onrender" not in calls[-1]


def test_requests_import_is_opt_in(monkeypatch):
    monkeypatch.delenv("REQUESTS_URL", raising=False)
    monkeypatch.delenv("ORG_REQUESTS_WORKSHEET_TITLE", raising=False)
    assert sources.fetch_requests() == ("off", None)

    class R:
        def json(self):
            return [{"Request ID": "REQ-1-001"}]
    monkeypatch.setenv("REQUESTS_URL", "https://example.org/requests")
    monkeypatch.setattr(sources, "_get", lambda url: R())
    assert sources.fetch_requests() == ("requests-url", [{"Request ID": "REQ-1-001"}])


def test_bad_service_account_key_is_not_echoed(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"private_key": "SECRET-123", "type": "service_account"}')
    with pytest.raises(sources.FeedError) as e:
        sources._gspread_client()
    assert "SECRET" not in str(e.value)
