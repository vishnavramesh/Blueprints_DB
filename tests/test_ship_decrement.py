"""PROPOSAL: marking a request Shipped takes the stock out of inventory (see docs/CHANGES-2026-10-11.md)."""
from b4p.sync import run_sync

BASE = {"Quantity": "8", "Manufacturer Name": "BD", "Date of Expiration": "2030-01-01", "Pallet Number": "1",
        "Box Number": "1", "Subcategory": "Gloves", "Image URL": "", "Lot Number": "L1"}


def _stock(conn, *items):
    rows = [(i + 2, dict(BASE, **it)) for i, it in enumerate(items)]
    run_sync(conn, rows, [])


def _qty(conn, name):
    return [r[0] for r in conn.execute(
        "SELECT quantity FROM inventory_item WHERE raw_name = %s ORDER BY item_id", (name,)).fetchall()]


def _request(conn, name, qty):
    rid = conn.execute("SELECT create_request('Clinic', 'c@example.org', %s::jsonb)",
                       (f'[{{"item_name": "{name}", "quantity": {qty}}}]',)).fetchone()[0]
    conn.execute("SELECT set_request_status(%s, 'Approved')", (rid,))
    return rid


def test_ship_takes_stock_once(conn):
    _stock(conn, {"Name": "Gauze"})
    rid = _request(conn, "Gauze", 5)
    assert _qty(conn, "Gauze") == [8]                       # approving changes nothing
    assert conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,)).fetchone()[0] is True
    assert _qty(conn, "Gauze") == [3]
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))   # again: no second decrement
    assert _qty(conn, "Gauze") == [3]
    picks = conn.execute("SELECT qty, short_qty FROM shipment_pick WHERE request_id = %s", (rid,)).fetchall()
    assert picks == [(5, 0)]


def test_short_line_floors_at_zero_and_is_recorded(conn):
    _stock(conn, {"Name": "Gauze", "Quantity": "3"})
    rid = _request(conn, "Gauze", 5)
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))
    assert _qty(conn, "Gauze") == [0]
    picks = conn.execute("SELECT item_id IS NULL, qty, short_qty FROM shipment_pick WHERE request_id = %s "
                         "ORDER BY pick_id", (rid,)).fetchall()
    assert picks == [(False, 3, 0), (True, 0, 2)]


def test_named_lot_box_pallet_comes_first_then_soonest_expiry(conn):
    _stock(conn,
           {"Name": "Gauze", "Quantity": "4", "Date of Expiration": "2027-01-01", "Lot Number": "A", "Box Number": "1"},
           {"Name": "Gauze", "Quantity": "4", "Date of Expiration": "2031-01-01", "Lot Number": "B", "Box Number": "2"},
           {"Name": "Gauze", "Quantity": "4", "Date of Expiration": "2029-01-01", "Lot Number": "C", "Box Number": "3"})
    rid = _request(conn, "Gauze [Lot B, Box 2, Pallet 1]", 6)
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))
    # lot B (4) first, then soonest expiry (lot A, 2027)
    assert _qty(conn, "Gauze") == [2, 0, 4]
    rid2 = _request(conn, "Gauze", 3)               # no suffix: soonest expiry among what is left
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid2,))
    assert _qty(conn, "Gauze") == [0, 0, 3]


def test_unknown_item_is_recorded_as_short_and_denied_requests_take_nothing(conn):
    _stock(conn, {"Name": "Gauze"})
    rid = _request(conn, "Mystery item", 2)
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))
    assert conn.execute("SELECT item_id IS NULL, short_qty FROM shipment_pick WHERE request_id = %s",
                        (rid,)).fetchall() == [(True, 2)]
    rid2 = conn.execute("SELECT create_request('Clinic', 'c@example.org', '[{\"item_name\": \"Gauze\", \"quantity\": 4}]')").fetchone()[0]
    conn.execute("SELECT set_request_status(%s, 'Denied')", (rid2,))
    assert _qty(conn, "Gauze") == [8]


def test_sheet_sync_keeps_the_decrement(conn):
    _stock(conn, {"Name": "Gauze"})
    rid = _request(conn, "Gauze", 5)
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))
    _stock(conn, {"Name": "Gauze"})                 # sheet still says 8, nobody edited the cell
    assert _qty(conn, "Gauze") == [3]
