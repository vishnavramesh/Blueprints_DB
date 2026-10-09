"""The dashboard contract (docs/CONTRACT.md): exact names, types and values."""
import gzip
import json
import re

import psycopg
import pytest
from psycopg.rows import dict_row

from b4p.sync import run_sync

from conftest import FIXTURES


@pytest.fixture
def loaded(conn, sheet_rows, request_rows):
    run_sync(conn, sheet_rows, request_rows)
    return conn


def columns(conn, view):
    """[(column, postgres type)] in order."""
    return conn.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
        (view,)).fetchall()


def rows(conn, sql, *args):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, args or None).fetchall()


# ---------------------------------------------------------------------------
def test_dash_items_shape(loaded):
    assert columns(loaded, "dash_items") == [
        ("id", "text"), ("name", "text"), ("gen_name", "text"), ("category", "text"),
        ("manufacturer", "text"), ("review_status", "text"), ("days_in_warehouse", "integer"),
        ("image_url", "text"), ("quantity", "integer"), ("unit_value", "double precision"),
        ("total_value", "double precision"), ("expiration_date", "text"), ("warehouse_date", "text"),
        ("pallet_number", "integer"), ("category_box", "text"), ("box_number", "integer"),
        ("sheet_row", "integer")]


def test_dash_items_matches_internal_dashboard_item_model(loaded):
    with gzip.open(FIXTURES / "internal_items_2026-09-06.json.gz", "rt") as f:
        model_keys = list(json.load(f)[0])
    got = rows(loaded, "SELECT * FROM dash_items LIMIT 1")[0]
    assert list(got) == model_keys + ["sheet_row"]


def test_dash_items_values(loaded, sheet_rows):
    items = rows(loaded, "SELECT * FROM dash_items")
    assert len(items) == 1136
    assert len({i["id"] for i in items}) == len(items)
    approved = {r[0] for r in loaded.execute("SELECT name::text FROM category")}
    for i in items:
        assert i["category"] in approved
        assert i["manufacturer"] and i["name"] and i["category_box"]
        assert i["gen_name"] is not None
        assert i["pallet_number"] >= 0 and i["box_number"] >= 0
        assert i["expiration_date"] is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", i["expiration_date"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", i["warehouse_date"])
        assert i["total_value"] == i["quantity"] * i["unit_value"]
    first = items[0]
    assert first == {
        "id": first["id"], "name": "62697 TELFA PACK UNIVERSITY OF MICHIGAN HOSP",
        "gen_name": "non-adherent dressing", "category": "Wound Care & Bandages",
        "manufacturer": "Cardinal Health", "review_status": "No", "days_in_warehouse": 0,
        "image_url": sheet_rows[0][1]["Image URL"], "quantity": 16, "unit_value": 0.0,
        "total_value": 0.0, "expiration_date": "2030-11-07", "warehouse_date": first["warehouse_date"],
        "pallet_number": 1, "category_box": "ST/N", "box_number": 4, "sheet_row": 2}
    # unknown manufacturer and unassigned box defaults
    assert any(i["manufacturer"] == "Unknown" for i in items)
    assert rows(loaded, "SELECT count(*) AS n FROM dash_items WHERE manufacturer = 'BD'")[0]["n"] > 100


def test_dash_items_supply_id_column(conn):
    """If the sheet gets a 'Supply ID' column, that becomes the id."""
    run_sync(conn, [(2, {"Supply ID": "S-77", "Name": "Gauze", "Quantity": "3", "Image URL": "u1"}),
                    (3, {"Supply ID": "", "Name": "Tape", "Quantity": "1", "Image URL": "u2"})], [])
    got = rows(conn, "SELECT id, name, category, manufacturer, category_box, box_number, pallet_number "
                     "FROM dash_items")
    assert got[0] == {"id": "S-77", "name": "Gauze", "category": "Misc Non-Medical",
                      "manufacturer": "Unknown", "category_box": "Unassigned", "box_number": 0,
                      "pallet_number": 0}
    assert got[1]["id"].isdigit()
    assert rows(conn, "SELECT * FROM set_item_quantity('S-77', 9)") == [
        {"id": "S-77", "quantity": 9, "total_value": 0.0, "sheet_row": 2}]


# ---------------------------------------------------------------------------
SUPPLY_COLS = ["Manufacturer Name", "Category", "Subcategory", "General", "Name", "Lot Number", "Notes",
               "Quantity", "Date of Expiration", "Pallet Number", "Box Number", "Image URL", "Review",
               "Days in Warehouse", "_sheet_row"]


def test_dash_supplies_shape(loaded):
    assert columns(loaded, "dash_supplies") == [(c, "integer" if c == "_sheet_row" else "text")
                                                for c in SUPPLY_COLS]


def test_dash_supplies_reproduces_every_sheet_cell(loaded, sheet_rows):
    got = {r["_sheet_row"]: r for r in rows(loaded, "SELECT * FROM dash_supplies")}
    nonblank = [(n, r) for n, r in sheet_rows if any(v.strip() for v in r.values())]
    assert len(got) == len(nonblank)
    for n, r in nonblank:
        for col in SUPPLY_COLS[:-1]:
            assert got[n][col] == r.get(col, ""), (n, col)
    assert all(v is not None for r in got.values() for v in r.values())


# ---------------------------------------------------------------------------
REQUEST_COLS = ["Request ID", "Org Name", "Org Email", "Item Name", "Category", "Quantity Requested",
                "Status", "Timestamp", "Review Flag"]


def test_dash_requests_shape(loaded):
    assert columns(loaded, "dash_requests") == [(c, "text") for c in REQUEST_COLS]


def test_dash_requests_reproduces_feed(loaded, request_rows):
    got = rows(loaded, "SELECT * FROM dash_requests")
    want = sorted(request_rows, key=lambda r: r["Timestamp"])
    assert got == want


# ---------------------------------------------------------------------------
def test_create_request(loaded):
    rid = loaded.execute("SELECT create_request(%s, %s, %s)", (
        "Example Clinic", "clinic@example.org",
        psycopg.types.json.Jsonb([
            {"item_name": "Gauze 4x4", "category": "Wound Care & Bandages", "quantity": 3, "review_flag": False},
            {"item_name": "Nitrile gloves M", "category": "Gloves", "quantity": 1, "review_flag": True},
            {"item_name": "Tape", "quantity": 2}]))).fetchone()[0]
    loaded.commit()
    assert re.fullmatch(r"REQ-\d{10}-\d{3}", rid)
    got = rows(loaded, 'SELECT * FROM dash_requests WHERE "Request ID" = %s', rid)
    assert [(g["Item Name"], g["Category"], g["Quantity Requested"], g["Review Flag"]) for g in got] == [
        ("Gauze 4x4", "Wound Care & Bandages", "3", "FALSE"), ("Nitrile gloves M", "Gloves", "1", "TRUE"),
        ("Tape", "", "2", "FALSE")]
    assert {g["Status"] for g in got} == {"Under Review"}
    assert {g["Org Name"] for g in got} == {"Example Clinic"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00", got[0]["Timestamp"])
    assert got == sorted(got, key=lambda g: g["Timestamp"])
    # newest last
    assert rows(loaded, "SELECT * FROM dash_requests")[-1]["Request ID"] == rid


@pytest.mark.parametrize("org,items", [
    ("", [{"item_name": "x", "quantity": 1}]),
    ("Org", []),
    ("Org", [{"item_name": "", "quantity": 1}]),
    ("Org", [{"item_name": "x", "quantity": 0}]),
    ("Org", {"item_name": "x", "quantity": 1}),
])
def test_create_request_rejects_bad_input_atomically(loaded, org, items):
    before = rows(loaded, "SELECT count(*) AS n FROM shipment")[0]["n"]
    with pytest.raises(psycopg.errors.RaiseException):
        loaded.execute("SELECT create_request(%s, 'a@b.c', %s)", (org, psycopg.types.json.Jsonb(items)))
    loaded.rollback()
    assert rows(loaded, "SELECT count(*) AS n FROM shipment")[0]["n"] == before


def test_set_request_status(loaded, request_rows):
    rid = request_rows[0]["Request ID"]
    for text, shown in [("Approved", "Approved"), ("shipped", "Shipped"), ("Delivered", "Delivered"),
                        ("Cancelled", "Cancelled"), ("Denied", "Denied"), ("Under Review", "Under Review")]:
        assert loaded.execute("SELECT set_request_status(%s, %s)", (rid, text)).fetchone()[0] is True
        assert {r["Status"] for r in rows(loaded, 'SELECT "Status" FROM dash_requests WHERE "Request ID" = %s',
                                          rid)} == {shown}
    assert loaded.execute("SELECT set_request_status('REQ-0-000', 'Shipped')").fetchone()[0] is False
    with pytest.raises(psycopg.errors.RaiseException):
        loaded.execute("SELECT set_request_status(%s, 'Lost in the mail')", (rid,))
    loaded.rollback()


def test_set_item_quantity(loaded):
    item = rows(loaded, "SELECT id FROM dash_items WHERE sheet_row = 3")[0]["id"]
    assert columns(loaded, "dash_items")  # sanity
    got = rows(loaded, "SELECT * FROM set_item_quantity(%s, 42)", item)
    assert got == [{"id": item, "quantity": 42, "total_value": 0.0, "sheet_row": 3}]
    assert rows(loaded, "SELECT * FROM set_item_quantity(%s, 0)", item) == [
        {"id": item, "quantity": 0, "total_value": 0.0, "sheet_row": 3}]  # ran out: allowed
    assert rows(loaded, 'SELECT "Quantity" FROM dash_supplies WHERE "_sheet_row" = 3')[0]["Quantity"] == "0"
    assert rows(loaded, "SELECT quantity FROM dash_items WHERE id = %s", item)[0]["quantity"] == 0
    assert rows(loaded, "SELECT * FROM set_item_quantity('no-such-id', 1)") == []
    with pytest.raises(psycopg.errors.RaiseException):
        loaded.execute("SELECT * FROM set_item_quantity(%s, -1)", (item,))
    loaded.rollback()
    # removed rows can't be edited
    loaded.execute("UPDATE inventory_item SET removed_at = now() WHERE item_id::text = %s", (item,))
    assert rows(loaded, "SELECT * FROM set_item_quantity(%s, 1)", item) == []
    loaded.rollback()


def test_create_request_import_is_idempotent(loaded):
    args = ("Old Clinic", "old@example.org",
            psycopg.types.json.Jsonb([{"item_name": "Saline", "category": "IV Supplies", "quantity": 4}]),
            "REQ-1700000000-123", "2023-11-14T22:13:20.5+00:00")
    assert loaded.execute("SELECT create_request(%s, %s, %s, %s, %s)", args).fetchone()[0] == "REQ-1700000000-123"
    again = (args[0], "x@example.org", psycopg.types.json.Jsonb([{"item_name": "Other", "quantity": 1}]),
             args[3], None)
    assert loaded.execute("SELECT create_request(%s, %s, %s, %s, %s)", again).fetchone()[0] == "REQ-1700000000-123"
    got = rows(loaded, 'SELECT * FROM dash_requests WHERE "Request ID" = %s', "REQ-1700000000-123")
    assert [(g["Item Name"], g["Org Email"], g["Timestamp"]) for g in got] == [
        ("Saline", "old@example.org", "2023-11-14T22:13:20.500000+00:00")]


def test_org_email_is_per_request(loaded):
    """Two people submitting under the same org name must each see only their own email."""
    items = psycopg.types.json.Jsonb([{"item_name": "Gauze", "quantity": 1}])
    a = loaded.execute("SELECT create_request('Shared Clinic', 'alice@example.org', %s)", (items,)).fetchone()[0]
    b = loaded.execute("SELECT create_request('Shared Clinic', 'bob@example.org', %s)", (items,)).fetchone()[0]
    mine = rows(loaded, 'SELECT "Request ID" FROM dash_requests WHERE lower("Org Email") = %s', "alice@example.org")
    assert [m["Request ID"] for m in mine] == [a]
    theirs = rows(loaded, 'SELECT "Request ID" FROM dash_requests WHERE lower("Org Email") = %s', "bob@example.org")
    assert [m["Request ID"] for m in theirs] == [b]
    assert rows(loaded, "SELECT count(*) AS n FROM requester WHERE org_name = 'Shared Clinic'")[0]["n"] == 1


def test_function_signatures(loaded):
    sigs = dict(loaded.execute(
        "SELECT p.proname, pg_get_function_identity_arguments(p.oid) || ' -> ' || "
        "pg_get_function_result(p.oid) FROM pg_proc p WHERE p.proname IN "
        "('create_request', 'set_request_status', 'set_item_quantity')").fetchall())
    assert sigs == {
        "create_request": "p_org_name text, p_org_email text, p_items jsonb, p_request_id text, "
                          "p_requested_at timestamp with time zone -> text",
        "set_request_status": "p_request_id text, p_status text -> boolean",
        "set_item_quantity": "p_id text, p_quantity integer -> TABLE(id text, quantity integer, "
                             "total_value double precision, sheet_row integer)",
    }
