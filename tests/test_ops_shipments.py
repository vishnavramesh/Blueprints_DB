"""Ops shipments (dash_shipment*, dash_allocations): allocation limits, stages, stock, privacy.

Synthetic stock only: no rows from the real inventory sheet.
"""
import json

import psycopg
import pytest
from psycopg.rows import dict_row

from b4p.sync import run_sync

BASE = {"Quantity": "10", "Manufacturer Name": "BD", "Date of Expiration": "2030-01-01", "Pallet Number": "2",
        "Box Number": "7", "Subcategory": "Gloves", "Image URL": "", "Lot Number": "L1", "Category": "ST/N"}


def _stock(conn, *items):
    run_sync(conn, [(i + 2, dict(BASE, **it)) for i, it in enumerate(items)], [])
    return [r[0] for r in conn.execute("SELECT id FROM dash_items ORDER BY sheet_row").fetchall()]


def _rows(conn, sql, *args):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, args or None).fetchall()


def _create(conn, lines, **header):
    h = {"name": "Clinic shipment", **header}
    sid = conn.execute("SELECT create_shipment(NULL, %s::jsonb, %s::jsonb)", (json.dumps(h), json.dumps(lines))).fetchone()[0]
    conn.commit()   # tests roll back failed calls; keep what already succeeded
    return sid


def _qty(conn, item):
    return conn.execute("SELECT quantity FROM dash_items WHERE id = %s", (item,)).fetchone()[0]


@pytest.fixture
def two(conn):
    return _stock(conn, {"Name": "Test gloves M"}, {"Name": "Test gauze", "Date of Expiration": "2026-01-01"})


def test_create_and_read_back(conn, two):
    a, b = two
    sid = _create(conn, [{"item_id": a, "quantity": 4}, {"item_id": b, "quantity": 2}], recipient="Test Clinic",
                  shipping_method="Freight", blueprints_contact="Alex Example", request_ids=[" REQ-1-AAA ", "REQ-1-AAA", "REQ-2"])
    assert sid.startswith("SHP-")
    h = _rows(conn, "SELECT * FROM dash_shipments WHERE id = %s", sid)[0]
    assert (h["stage"], h["recipient"], h["shipping_method"], h["blueprints_contact"]) == ("planned", "Test Clinic", "Freight", "Alex Example")
    assert h["request_ids"] == ["REQ-1-AAA", "REQ-2"]
    lines = {r["item_id"]: r for r in _rows(conn, "SELECT * FROM dash_shipment_lines WHERE shipment_id = %s", sid)}
    g = lines[a]
    assert (g["quantity"], g["on_hand"], g["available"], g["max_quantity"]) == (4, 10, 6, 10)
    assert (g["pallet_number"], g["category_box"], g["box_number"], g["lot_number"]) == (2, "ST/N", 7, "L1")
    assert g["expiry_status"] == "later"
    assert lines[b]["expiry_status"] == "expired"


def test_allocations_view_lists_active_only(conn, two):
    a, _ = two
    s1 = _create(conn, [{"item_id": a, "quantity": 3}], name="One")
    s2 = _create(conn, [{"item_id": a, "quantity": 2}], name="Two")
    got = {(r["shipment_id"], r["quantity"], r["stage"]) for r in _rows(conn, "SELECT * FROM dash_allocations WHERE item_id = %s", a)}
    assert got == {(s1, 3, "planned"), (s2, 2, "planned")}
    conn.execute("SELECT update_shipment(%s, '{\"stage\": \"shipped\"}')", (s1,))
    got = _rows(conn, "SELECT shipment_id FROM dash_allocations WHERE item_id = %s", a)
    assert [r["shipment_id"] for r in got] == [s2]


def test_over_allocation_is_rejected_with_clear_error(conn, two):
    a, _ = two
    _create(conn, [{"item_id": a, "quantity": 7}], name="One")
    with pytest.raises(psycopg.errors.CheckViolation) as e:
        _create(conn, [{"item_id": a, "quantity": 4}], name="Two")
    assert "only 3 available" in str(e.value)
    assert json.loads(e.value.diag.message_detail) == {"item_id": a, "requested": 4, "available": 3}
    conn.rollback()
    # the failed create wrote nothing
    assert conn.execute("SELECT count(*) FROM dash_shipment").fetchone()[0] == 1


def test_over_allocation_in_one_shipment_and_on_edit(conn, two):
    a, _ = two
    with pytest.raises(psycopg.errors.CheckViolation):
        _create(conn, [{"item_id": a, "quantity": 11}])
    conn.rollback()
    sid = _create(conn, [{"item_id": a, "quantity": 5}])
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("SELECT set_shipment_lines(%s, %s::jsonb)", (sid, json.dumps([{"item_id": a, "quantity": 11}])))
    conn.rollback()
    assert conn.execute("SELECT set_shipment_lines(%s, %s::jsonb)", (sid, json.dumps([{"item_id": a, "quantity": 10}]))).fetchone()[0] == 1


def test_lowering_a_quantity_is_allowed_even_if_the_sheet_dropped_stock(conn, two):
    a, _ = two
    sid = _create(conn, [{"item_id": a, "quantity": 8}])
    conn.execute("SELECT set_item_quantity(%s, 5)", (a,))      # now over-allocated by 3
    assert _rows(conn, "SELECT available FROM dash_shipment_lines WHERE shipment_id = %s", sid)[0]["available"] == -3
    conn.execute("SELECT set_shipment_lines(%s, %s::jsonb)", (sid, json.dumps([{"item_id": a, "quantity": 6}])))
    with pytest.raises(psycopg.errors.CheckViolation):          # but cannot ship more than is there
        conn.execute("SELECT update_shipment(%s, '{\"stage\": \"shipped\"}')", (sid,))


def test_lines_keep_ids_and_flags_and_bad_input(conn, two):
    a, b = two
    sid = _create(conn, [{"item_id": a, "quantity": 2}, {"item_id": b, "quantity": 1}])
    lid = _rows(conn, "SELECT id FROM dash_shipment_lines WHERE shipment_id = %s AND item_id = %s", sid, a)[0]["id"]
    assert conn.execute("SELECT set_shipment_line_flags(%s, true, NULL)", (int(lid),)).fetchone()[0] is True
    conn.execute("SELECT set_shipment_lines(%s, %s::jsonb)", (sid, json.dumps([{"item_id": a, "quantity": 3}])))
    got = _rows(conn, "SELECT id, quantity, picked FROM dash_shipment_lines WHERE shipment_id = %s", sid)
    assert got == [{"id": lid, "quantity": 3, "picked": True}]
    assert conn.execute("SELECT set_shipment_line_flags(999999, true, NULL)").fetchone()[0] is False
    for bad in ([{"item_id": a, "quantity": 0}], [{"item_id": a, "quantity": "2"}], [{"item_id": "nope", "quantity": 1}],
                [{"item_id": a, "quantity": 1}, {"item_id": a, "quantity": 1}]):
        with pytest.raises((psycopg.errors.RaiseException, psycopg.errors.NoDataFound)):
            conn.execute("SELECT set_shipment_lines(%s, %s::jsonb)", (sid, json.dumps(bad)))
        conn.rollback()
        sid = _create(conn, [{"item_id": a, "quantity": 2}])


def test_stage_moves_and_stock_is_taken_once(conn, two):
    a, _ = two
    sid = _create(conn, [{"item_id": a, "quantity": 4}])
    up = lambda st: conn.execute("SELECT update_shipment(%s, %s::jsonb)", (sid, json.dumps({"stage": st}))).fetchone()[0]
    assert up("pulling") and up("consolidated") and up("pulling") and up("planned") and up("consolidated")   # one step back is fine
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="cannot change shipment"):
        up("planned")                                                                            # two steps back is not
    conn.rollback()
    assert _qty(conn, a) == 10
    assert up("shipped") is True
    assert _qty(conn, a) == 6
    conn.commit()
    h = _rows(conn, "SELECT ship_date FROM dash_shipments WHERE id = %s", sid)[0]
    assert h["ship_date"] is not None
    assert up("shipped") is True and _qty(conn, a) == 6                   # same stage: no second decrement
    for st in ("consolidated", "planned"):
        with pytest.raises(psycopg.errors.RaiseException, match="cannot change shipment"):
            up(st)
        conn.rollback()
    assert up("delivered") is True and _qty(conn, a) == 6
    assert _rows(conn, "SELECT delivered_date FROM dash_shipments WHERE id = %s", sid)[0]["delivered_date"] is not None
    with pytest.raises(psycopg.errors.RaiseException, match="cannot change the lines"):
        conn.execute("SELECT set_shipment_lines(%s, '[]')", (sid,))
    conn.rollback()
    # a sheet sync keeps the decremented quantity
    run_sync(conn, [(i + 2, dict(BASE, **it)) for i, it in enumerate([{"Name": "Test gloves M"}, {"Name": "Test gauze", "Date of Expiration": "2026-01-01"}])], [])
    assert _qty(conn, a) == 6


def test_skipping_straight_to_delivered_takes_stock(conn, two):
    a, _ = two
    sid = _create(conn, [{"item_id": a, "quantity": 4}])
    conn.execute("SELECT update_shipment(%s, '{\"stage\": \"delivered\"}')", (sid,))
    assert _qty(conn, a) == 6


def test_header_patch_validation(conn, two):
    sid = _create(conn, [])
    assert conn.execute("SELECT update_shipment('SHP-nope', '{\"notes\": \"x\"}')").fetchone()[0] is False
    conn.execute("SELECT update_shipment(%s, %s::jsonb)", (sid, json.dumps({"shipping_method": "Courier", "ship_date": "2026-11-02", "request_ids": ["R1"]})))
    h = _rows(conn, "SELECT * FROM dash_shipments WHERE id = %s", sid)[0]
    assert (h["shipping_method"], h["ship_date"], h["request_ids"]) == ("Courier", "2026-11-02", ["R1"])
    conn.execute("SELECT update_shipment(%s, '{\"ship_date\": null, \"request_ids\": []}')", (sid,))
    h = _rows(conn, "SELECT * FROM dash_shipments WHERE id = %s", sid)[0]
    assert (h["ship_date"], h["request_ids"], h["shipping_method"]) == (None, [], "Courier")
    for bad in ({"stage": "lost"}, {"ship_date": "2026-02-30"}, {"ship_date": "soon"}, {"name": ""}, {"bogus": 1},
                {"request_ids": "R1"}, {"request_ids": [1]}, {"name": 5}):
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("SELECT update_shipment(%s, %s::jsonb)", (sid, json.dumps(bad)))
        conn.rollback()
        sid = _create(conn, [])


def test_create_is_idempotent_by_id_and_import_keeps_history_without_touching_stock(conn, two):
    a, _ = two
    hdr = json.dumps({"name": "Old one", "stage": "delivered", "ship_date": "2026-03-01", "created_at": "2026-02-20T10:00:00Z"})
    lines = json.dumps([{"item_id": a, "quantity": 99}, {"item_id": "424242", "quantity": 1}])   # more than on hand: history
    for _ in range(2):
        assert conn.execute("SELECT create_shipment('old-1', %s::jsonb, %s::jsonb, true)", (hdr, lines)).fetchone()[0] == "old-1"
    assert conn.execute("SELECT count(*) FROM dash_shipment WHERE id = 'old-1'").fetchone()[0] == 1
    assert _qty(conn, a) == 10
    h = _rows(conn, "SELECT * FROM dash_shipments WHERE id = 'old-1'")[0]
    assert (h["stage"], h["ship_date"], h["delivered_date"], h["created_at"][:10]) == ("delivered", "2026-03-01", "2026-03-01", "2026-02-20")
    assert [r["quantity"] for r in _rows(conn, "SELECT quantity FROM dash_shipment_lines WHERE shipment_id = 'old-1'")] == [99]
    assert _rows(conn, "SELECT * FROM dash_allocations") == []
    with pytest.raises(psycopg.errors.RaiseException):            # not allowed outside import
        conn.execute("SELECT create_shipment(NULL, '{\"name\": \"x\", \"stage\": \"shipped\"}', '[]')")


def test_privacy_free_text_in_shipment_views_is_redacted(conn):
    a, = _stock(conn, {"Name": "SERUM TUBE Pt. ID " "8675309111 QQ11", "Lot Number": "Pt. ID " "5551110000"})
    sid = _create(conn, [{"item_id": a, "quantity": 1}], name="For patient: Jane Doe", recipient="call 555-010-0142",
                  notes="DOB " "03/04/1961 mail jane.doe@example.org", blueprints_contact="Pt. ID " "8675309111 QQ11",
                  request_ids=["jane.doe@example.org"])
    blob = json.dumps([_rows(conn, f"SELECT * FROM {v}") for v in ("dash_shipments", "dash_shipment_lines", "dash_allocations")], default=str)
    for secret in ("8675309111", "5551110000", "Jane", "555-010-0142", "1961", "jane.doe@example.org"):
        assert secret not in blob, secret
    # the tables keep the original for HQ
    assert "Jane" in conn.execute("SELECT name FROM dash_shipment WHERE id = %s", (sid,)).fetchone()[0]


def _cols(conn, view):
    return [c.name for c in conn.execute(f"SELECT * FROM {view} LIMIT 0").description]


def test_view_shapes_are_the_contract(conn):
    assert _cols(conn, "dash_allocations") == ["item_id", "shipment_id", "shipment_name", "stage", "quantity"]
    assert _cols(conn, "dash_shipments") == [
        "id", "name", "recipient", "shipment_type", "shipping_method", "blueprints_contact", "stage", "ship_date",
        "delivered_date", "request_ids", "notes", "kind", "unit_count", "value", "created_at", "updated_at"]
    assert _cols(conn, "dash_shipment_lines") == [
        "id", "shipment_id", "item_id", "quantity", "picked", "consolidated", "name", "category", "lot_number",
        "expiration_date", "pallet_number", "category_box", "box_number", "unit_price", "on_hand", "allocated_active", "available",
        "max_quantity", "expiry_status"]


def test_function_signatures(conn):
    sigs = dict(conn.execute(
        "SELECT p.proname, pg_get_function_identity_arguments(p.oid) || ' -> ' || pg_get_function_result(p.oid) "
        "FROM pg_proc p WHERE p.proname IN ('create_shipment', 'update_shipment', 'set_shipment_lines', "
        "'set_shipment_line_flags')").fetchall())
    assert sigs == {
        "create_shipment": "p_id text, p_header jsonb, p_lines jsonb, p_import boolean -> text",
        "update_shipment": "p_id text, p_patch jsonb -> boolean",
        "set_shipment_lines": "p_id text, p_lines jsonb -> integer",
        "set_shipment_line_flags": "p_line_id bigint, p_picked boolean, p_consolidated boolean -> boolean",
    }


def test_free_text_lines_skip_inventory_checks_and_stock(conn, two):
    a, _ = two
    sid = _create(conn, [{"item_id": a, "quantity": 3}, {"description": "Donated crutches", "quantity": 500, "unit_price": 12.5,
                                                        "lot_number": "X9", "expiration_date": "2031-05-01"}])
    ls = {r["name"]: r for r in _rows(conn, "SELECT * FROM dash_shipment_lines WHERE shipment_id = %s", sid)}
    f = ls["Donated crutches"]
    assert (f["item_id"], f["on_hand"], f["available"], f["max_quantity"], f["pallet_number"]) == (None, None, None, None, None)
    assert (f["quantity"], f["unit_price"], f["lot_number"], f["expiration_date"], f["expiry_status"]) == (500, 12.5, "X9", "2031-05-01", "later")
    assert _rows(conn, "SELECT item_id FROM dash_allocations") == [{"item_id": a}]
    # replacing the lines replaces free-text lines; a line needs an item or a description
    assert conn.execute("SELECT set_shipment_lines(%s, %s::jsonb)", (sid, json.dumps(
        [{"item_id": a, "quantity": 3}, {"description": "Crutches", "quantity": 1}, {"description": "Walker", "quantity": 2}]))).fetchone()[0] == 3
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("SELECT set_shipment_lines(%s, %s::jsonb)", (sid, json.dumps([{"quantity": 1}])))
    conn.rollback()
    conn.execute("SELECT update_shipment(%s, '{\"stage\": \"shipped\"}')", (sid,))
    assert _qty(conn, a) == 7        # only the inventory line took stock


def test_non_inventory_shipment_and_history_snapshot_lines(conn, two):
    a, _ = two
    hdr = {"name": "Walk-in", "kind": "non_inventory", "stage": "shipped", "ship_date": "2026-02-01", "value": 1250.5, "unit_count": 40}
    sid = conn.execute("SELECT create_shipment('non-inventory-shipment-1', %s::jsonb, '[]', true)", (json.dumps(hdr),)).fetchone()[0]
    h = _rows(conn, "SELECT * FROM dash_shipments WHERE id = %s", sid)[0]
    assert (h["kind"], h["value"], h["unit_count"], h["stage"]) == ("non_inventory", 1250.5, 40, "shipped")
    lines = [{"item_id": "424242", "name": "Old gloves M", "quantity": 40, "lot_number": "L1", "expiration_date": "2027-01-31", "unit_price": 0.12},
             {"item_id": a, "name": "Test gloves M", "quantity": 2}]
    conn.execute("SELECT create_shipment('shipment-1', '{\"name\": \"A\", \"stage\": \"delivered\"}', %s::jsonb, true)", (json.dumps(lines),))
    got = {r["name"]: r for r in _rows(conn, "SELECT * FROM dash_shipment_lines WHERE shipment_id = 'shipment-1'")}
    old = got["Old gloves M"]
    assert (old["item_id"], old["lot_number"], old["expiration_date"], old["unit_price"], old["quantity"]) == (None, "L1", "2027-01-31", 0.12, 40)
    assert got["Test gloves M"]["item_id"] == a and _qty(conn, a) == 10
    for bad in ({"kind": "other"}, {"value": -1}, {"unit_count": 1.5}, {"value": "5"}):
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("SELECT update_shipment(%s, %s::jsonb)", (sid, json.dumps(bad)))
        conn.rollback()


def _approved_request(conn, name, qty):
    rid = conn.execute("SELECT create_request('Clinic', 'c@example.org', %s::jsonb)",
                       (json.dumps([{"item_name": name, "quantity": qty}]),)).fetchone()[0]
    conn.execute("SELECT set_request_status(%s, 'Approved')", (rid,))
    return rid


def test_linked_request_and_shipment_take_stock_once_in_every_order(conn):
    a, = _stock(conn, {"Name": "Test gauze"})
    # 1. shipment linked first: request Shipped takes nothing, the shipment takes it when it ships
    rid = _approved_request(conn, "Test gauze", 4)
    sid = _create(conn, [{"item_id": a, "quantity": 4}], request_ids=[rid])
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))
    assert _qty(conn, a) == 10
    conn.execute("SELECT update_shipment(%s, '{\"stage\": \"shipped\"}')", (sid,))
    assert _qty(conn, a) == 6
    conn.execute("SELECT set_request_status(%s, 'Delivered')", (rid,))
    assert _qty(conn, a) == 6
    # 2. shipment shipped first, request linked and marked Shipped afterwards
    rid2 = _approved_request(conn, "Test gauze", 2)
    sid2 = _create(conn, [{"item_id": a, "quantity": 2}], request_ids=[rid2])
    conn.execute("SELECT update_shipment(%s, '{\"stage\": \"shipped\"}')", (sid2,))
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid2,))
    assert _qty(conn, a) == 4


def test_request_shipped_before_it_was_linked_is_credited(conn):
    a, = _stock(conn, {"Name": "Test gauze"})
    rid = _approved_request(conn, "Test gauze", 3)
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))
    assert _qty(conn, a) == 7
    sid = _create(conn, [{"item_id": a, "quantity": 5}], request_ids=[rid])      # 3 of the 5 already left with the request
    conn.execute("SELECT update_shipment(%s, '{\"stage\": \"shipped\"}')", (sid,))
    assert _qty(conn, a) == 5                                                    # only the other 2 are taken
    assert conn.execute("SELECT claimed_by FROM shipment_pick WHERE request_id = %s", (rid,)).fetchone()[0] == sid
    sid2 = _create(conn, [{"item_id": a, "quantity": 1}], request_ids=[rid])     # the credit is used up
    conn.execute("SELECT update_shipment(%s, '{\"stage\": \"shipped\"}')", (sid2,))
    assert _qty(conn, a) == 4


def test_migration_is_one_transaction():
    from b4p.db import SCHEMA
    lines = [ln.strip() for ln in SCHEMA.read_text().splitlines()]
    stmts = [ln for ln in lines if ln.rstrip(";").upper() in ("BEGIN", "COMMIT") and ln.endswith(";")]
    assert stmts == ["BEGIN;", "COMMIT;"]
    assert [ln for ln in lines if ln and not ln.startswith("--")][-1] == "COMMIT;"


def test_failed_migration_leaves_old_views_in_place(test_db):
    from b4p import db
    sql = db.SCHEMA.read_text().replace("\nCOMMIT;", "\nSELECT 1/0;\nCOMMIT;")
    with db.connect(test_db) as c:
        with pytest.raises(psycopg.errors.DivisionByZero):
            c.execute(sql)
        c.rollback()
        assert c.execute("SELECT count(*) FROM pg_views WHERE viewname IN ('dash_shipments', 'dash_shipment_lines', 'dash_allocations')").fetchone()[0] == 3


def test_non_inventory_shipment_can_be_created_directly_as_shipped(conn, two):
    a, _ = two
    hdr = {"name": "Walk-in", "kind": "non_inventory", "stage": "shipped", "ship_date": "2026-02-01", "value": 99}
    sid = conn.execute("SELECT create_shipment('client-key-1', %s::jsonb, '[]')", (json.dumps(hdr),)).fetchone()[0]
    assert sid == "client-key-1"
    assert conn.execute("SELECT create_shipment('client-key-1', %s::jsonb, '[]')", (json.dumps(hdr),)).fetchone()[0] == sid   # retry: no duplicate
    assert conn.execute("SELECT count(*) FROM dash_shipment").fetchone()[0] == 1
    h = _rows(conn, "SELECT stage, ship_date, value FROM dash_shipments WHERE id = %s", sid)[0]
    assert (h["stage"], h["ship_date"], h["value"]) == ("shipped", "2026-02-01", 99)
    assert _qty(conn, a) == 10
    with pytest.raises(psycopg.errors.RaiseException):          # inventory shipments still start planned
        conn.execute("SELECT create_shipment(NULL, '{\"name\": \"x\", \"stage\": \"shipped\"}', '[]')")
