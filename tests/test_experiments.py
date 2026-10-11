"""Ops report and public impact export: content and privacy."""
import json

import pytest

from b4p import cli, impact, report, sources
from conftest import load_requests, load_sheet_csv


@pytest.fixture
def synced(conn, monkeypatch):
    monkeypatch.setattr(sources, "fetch_inventory", lambda **_: ("fixture", load_sheet_csv()))
    monkeypatch.setattr(sources, "fetch_requests", lambda **_: ("fixture", load_requests()))
    assert cli.main(["sync"]) == 0
    return conn


def _personal_values():
    vals = set()
    for r in load_requests():
        for field in ("Org Email", "Org Name"):
            v = str(r.get(field) or "").strip().lower()
            if v:
                vals.add(v)
    assert vals
    return vals


# ---------------------------------------------------------------------------
# the report relies on the database's redact_phi
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,secret", [
    ("SM SERUM MARKER PRE Pt. ID 8675309111-1 extra", "8675309111"),
    ("PT.ID#555123", "555123"),
    ("MRN 00123456", "00123456"),
    ("DOB 01/02/1960", "1960"),
    ("Call Dr. Smith 734-555-0100", "555-0100"),
    ("(734) 555-0102", "555-0102"),
    ("john.smith@umich.edu", "umich"),
])
def test_redact_phi_masks_report_strings(conn, text, secret):
    out = conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0]
    assert secret not in out.lower()


def test_redact_phi_keeps_ordinary_text(conn):
    s = "Gauze pads, sterile 4x4 (box of 50) stored at warehouse 2"
    assert conn.execute("SELECT redact_phi(%s)", (s,)).fetchone()[0] == s


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
HOSTILE = [
    (1, '<script>alert(1)</script><img src=x onerror=alert(2)>', 'Yes'),
    (2, 'AAA ![t](https://evil.test/track.png) [click](javascript:alert(1))', 'No'),
    (3, 'AAA jane.doe at umich dot edu / jane(at)umich.edu / jane@umich / ｊａｎｅ＠ｕｍｉｃｈ．ｅｄｕ', 'No'),
    (4, 'AAA Call Dr. Smith 734-555-0100, MRN 00123456, DOB 01/02/1960',
     'Yes - ask John Smith (john.smith@umich.edu) 734 555 0101'),
    (5, 'SM SERUM MARKER PRE Pt. ID 8675309111-1', 'No'),
]


def _inject(conn):
    for item_id, name, review in HOSTILE:
        conn.execute("UPDATE inventory_item SET raw_name = %s, supply_id = NULL, quantity = 1, review = %s, "
                     "flagged = true, reviewed_at = NULL WHERE item_id = %s", (name, review, item_id))
    conn.commit()


def test_report_withholds_and_masks_hostile_cells(synced, tmp_path):
    _inject(synced)
    text = report.write_report(synced, tmp_path / "ops.html").read_text()
    low = text.lower()
    for secret in ("8675309111", "serum marker", "555-0100", "555 0101", "00123456", "1960", "umich",
                   "jane", "john.smith"):
        assert secret not in low, secret
    assert "<script" not in low and "<img" not in low and "<a " not in low
    assert "specimen items withheld" in low


def test_report_has_sections_and_no_requester_data(synced, tmp_path):
    out = report.write_report(synced, tmp_path / "ops.html")
    text = out.read_text()
    for heading in ("Already expired", "Expiring within 30 days", "Expiring in 31 to 90 days", "Low stock",
                    "Review queue", "Boxes and pallets", "Requests by status", "Last sync"):
        assert heading in text
    assert "Under Review" in text
    for v in _personal_values():
        assert v not in text.lower()
    assert "@" not in text
    import re
    assert not re.search(r"\bpt\.?\s*id\b|\d{10}", text, re.I)  # real fixture has specimen tubes
    assert list(tmp_path.iterdir()) == [out]  # no Markdown twin


def test_report_masks_emails_in_warnings(synced, tmp_path):
    synced.execute("UPDATE sync_run SET warnings = '[\"row 5: contact jane@example.org\"]'::jsonb")
    synced.commit()
    text = report.write_report(synced, tmp_path / "r.html").read_text()
    assert "jane@example.org" not in text and "[email redacted]" in text


def test_report_box_count_matches_distinct_type_and_number(synced, tmp_path):
    d = report.collect(synced)
    expected = dict(synced.execute(
        "SELECT pallet_number, count(DISTINCT (box_type, box_number)) FROM v_inventory "
        "WHERE pallet_number IS NOT NULL GROUP BY 1").fetchall())
    assert {p: b for p, b, _, _ in d["boxes"]} == expected


def test_report_on_empty_database(conn, tmp_path):
    out = report.write_report(conn, tmp_path / "empty.html")
    assert "No sync has run yet" in out.read_text()


def test_cli_report_default_name(synced, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["report"]) == 0
    files = sorted(p.name for p in (tmp_path / "reports").iterdir())
    assert len(files) == 1 and files[0].startswith("ops-") and files[0].endswith(".html")


def test_workflow_does_not_upload_the_report():
    from pathlib import Path
    wf = (Path(__file__).parent.parent / ".github" / "workflows" / "sync.yml").read_text()
    assert "upload-artifact" not in wf and "b4p report" not in wf


# ---------------------------------------------------------------------------
# public impact
# ---------------------------------------------------------------------------
def test_public_impact_is_aggregate_only(synced, tmp_path):
    cols = [r[0] for r in synced.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'public_impact'")]
    assert set(cols) == {"units_on_hand", "distinct_products", "categories", "units_shipped",
                         "requests_fulfilled", "partner_orgs", "last_updated"}
    out = tmp_path / "impact.json"
    assert cli.main(["impact", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert set(data) == {"unitsOnHand", "distinctProducts", "categories", "unitsShipped",
                         "requestsFulfilled", "partnerOrgs", "lastUpdated"}
    good = synced.execute(
        "SELECT COALESCE(sum(quantity), 0) FROM inventory_item WHERE removed_at IS NULL "
        "AND NOT (flagged AND reviewed_at IS NULL) "
        "AND (expiration_date IS NULL OR expiration_date >= CURRENT_DATE)").fetchone()[0]
    all_units = synced.execute("SELECT total_raw_items FROM v_kpis").fetchone()[0]
    assert data["unitsOnHand"] == good < all_units
    assert sum(c["units"] for c in data["categories"]) == good
    text = out.read_text().lower()
    for v in _personal_values():
        assert v not in text


def test_impact_last_updated_is_last_good_sync(synced):
    ts = synced.execute("SELECT max(finished_at) FROM sync_run WHERE error IS NULL").fetchone()[0]
    assert impact.fetch(synced)["lastUpdated"] == ts.isoformat(timespec="seconds")
    synced.execute("INSERT INTO sync_run (finished_at, error) VALUES (now() + interval '1 hour', 'boom')")
    synced.commit()
    assert impact.fetch(synced)["lastUpdated"] == ts.isoformat(timespec="seconds")


def test_impact_hides_shipping_figures_below_three_partners(synced):
    def deliver(n):
        synced.execute("UPDATE shipment SET status = 'Under Review'")
        synced.execute("UPDATE shipment SET status = 'Delivered' WHERE shipment_id IN "
                       "(SELECT min(shipment_id) FROM shipment GROUP BY requester_id "
                       "ORDER BY min(shipment_id) LIMIT %s)", (n,))
        synced.commit()
        return impact.fetch(synced)

    for n in (0, 1, 2):
        d = deliver(n)
        assert d["partnerOrgs"] is None and d["unitsShipped"] is None and d["requestsFulfilled"] is None
    d = deliver(3)
    assert d["partnerOrgs"] == 3 and d["requestsFulfilled"] == 3 and d["unitsShipped"] > 0
