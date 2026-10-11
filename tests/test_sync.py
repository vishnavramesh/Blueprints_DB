"""Sync behaviour against the real snapshots."""
import copy

from b4p.sync import run_sync

from conftest import load_old_supplies, load_requests


def count(conn, sql, *args):
    return conn.execute(sql, args or None).fetchone()[0]


def test_initial_load_matches_sheet(conn, sheet_rows, request_rows):
    s = run_sync(conn, sheet_rows, request_rows, source="fixture")
    assert s["rows_seen"] == len(sheet_rows) == 1137
    assert s["rows_skipped"] == 1  # one fully blank row (sheet row 208)
    assert s["items_added"] == 1136
    assert count(conn, "SELECT count(*) FROM dash_items") == 1136
    assert count(conn, "SELECT count(*) FROM dash_supplies") == 1136
    ids = {r["Request ID"] for r in request_rows}
    assert s["requests_added"] == len(ids) == 12
    assert s["lines_added"] == len(request_rows) == 20
    assert count(conn, "SELECT count(*) FROM dash_requests") == 20
    assert count(conn, "SELECT count(*) FROM sync_run") == 1


def test_resync_is_a_noop(conn, sheet_rows, request_rows):
    run_sync(conn, sheet_rows, request_rows)
    before = conn.execute("SELECT max(xmin::text::bigint) FROM inventory_item").fetchone()[0]
    s = run_sync(conn, sheet_rows, request_rows)
    assert (s["items_added"], s["items_updated"], s["items_removed"]) == (0, 0, 0)
    assert (s["requests_added"], s["lines_added"]) == (0, 0)
    after = conn.execute("SELECT max(xmin::text::bigint) FROM inventory_item").fetchone()[0]
    assert before == after  # not a single row rewritten
    assert count(conn, "SELECT count(*) FROM inventory_item") == 1136


def test_edited_row_is_updated_in_place(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    rows = copy.deepcopy(sheet_rows)
    rows[0][1]["Quantity"] = "30"
    rows[0][1]["Subcategory"] = "Gloves"
    s = run_sync(conn, rows, [])
    assert (s["items_added"], s["items_updated"], s["items_removed"]) == (0, 1, 0)
    q, cat = conn.execute("SELECT quantity, category FROM dash_items WHERE sheet_row = 2").fetchone()
    assert (q, cat) == (30, "Gloves")
    assert count(conn, "SELECT count(*) FROM inventory_item") == 1136


def test_removed_row_and_comeback(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    gone_url = sheet_rows[5][1]["Image URL"]
    rows = [r for i, r in enumerate(sheet_rows) if i != 5]
    s = run_sync(conn, rows, [])
    assert s["items_removed"] == 1
    assert count(conn, "SELECT count(*) FROM dash_items") == 1135
    assert count(conn, "SELECT count(*) FROM inventory_item WHERE image_url = %s AND removed_at IS NOT NULL",
                 gone_url) == 1
    s = run_sync(conn, sheet_rows, [])  # row is back
    assert (s["items_added"], s["items_updated"]) == (0, 1)
    assert count(conn, "SELECT count(*) FROM dash_items") == 1136


def test_deleting_a_row_shifts_sheet_rows(conn, sheet_rows):
    """Deleting a row in the sheet renumbers the rows below it; _sheet_row must follow."""
    run_sync(conn, sheet_rows, [])
    rows = [(n if n < 3 else n - 1, r) for n, r in sheet_rows if n != 3]
    run_sync(conn, rows, [])
    url = sheet_rows[2][1]["Image URL"]  # was sheet row 4
    assert count(conn, 'SELECT "_sheet_row" FROM dash_supplies WHERE "Image URL" = %s', url) == 3


def test_tiny_feed_does_not_wipe_inventory(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    s = run_sync(conn, sheet_rows[:10], [])
    assert s["items_removed"] == 0
    assert any("NOTHING was marked removed" in w for w in s["warnings"])
    assert count(conn, "SELECT count(*) FROM dash_items") == 1136


def test_feed_never_changes_existing_requests(conn, request_rows):
    """Insert-only: once a request is in the database, the feed can't change it."""
    run_sync(conn, [], request_rows)
    rows = copy.deepcopy(request_rows)
    rid = rows[0]["Request ID"]
    for r in rows:
        if r["Request ID"] == rid:
            r["Status"] = "Shipped"
            r["Org Email"] = "someone-else@example.org"
    extra = {**rows[0], "Item Name": "Extra line", "Quantity Requested": "7"}
    s = run_sync(conn, [], rows + [extra])
    assert (s["requests_added"], s["lines_added"]) == (0, 0)
    got = conn.execute('SELECT DISTINCT "Status", "Org Email" FROM dash_requests WHERE "Request ID" = %s',
                       (rid,)).fetchall()
    assert got == [("Under Review", request_rows[0]["Org Email"])]


def test_feed_does_not_undo_dashboard_status(conn, request_rows):
    run_sync(conn, [], request_rows)
    rid = request_rows[0]["Request ID"]
    assert conn.execute("SELECT set_request_status(%s, 'Approved')", (rid,)).fetchone()[0]
    conn.commit()
    run_sync(conn, [], request_rows)
    assert conn.execute("SELECT status FROM shipment WHERE request_id = %s", (rid,)).fetchone()[0] == "Approved"


def test_new_request_imported_with_its_status_and_time(conn, request_rows):
    new = [{**request_rows[0], "Request ID": "REQ-1700000000-001", "Status": "Approved",
            "Timestamp": "2023-11-14T22:13:20+00:00", "Quantity Requested": "0"}]
    s = run_sync(conn, [], new)
    assert (s["requests_added"], s["lines_added"]) == (1, 1)
    assert any("imported as 1" in w for w in s["warnings"])
    got = conn.execute('SELECT "Status", "Timestamp", "Quantity Requested" FROM dash_requests').fetchall()
    assert got == [("Approved", "2023-11-14T22:13:20+00:00", "1")]
    assert conn.execute("SELECT source FROM shipment").fetchone()[0] == "feed"


def test_messy_request_row_still_imports(conn, sheet_rows, request_rows):
    messy = {**request_rows[0], "Request ID": "REQ-1-001", "Item Name": "   ", "Status": "Lost",
             "Timestamp": "yesterday"}
    s = run_sync(conn, sheet_rows, request_rows + [messy])
    assert s["requests_added"] == 13 and not s["errors"]
    assert any("unknown status" in w for w in s["warnings"])
    got = conn.execute('SELECT "Item Name", "Status" FROM dash_requests WHERE "Request ID" = %s',
                       ("REQ-1-001",)).fetchall()
    assert got == [("?", "Under Review")]


def test_sync_never_touches_app_requests(conn, request_rows):
    run_sync(conn, [], request_rows)
    rid = conn.execute("""SELECT create_request('Test Org', 'test@example.org',
                          '[{"item_name": "Gauze", "category": "Wound Care & Bandages", "quantity": 2}]')""").fetchone()[0]
    conn.execute("SELECT set_request_status(%s, 'Shipped')", (rid,))
    conn.commit()
    # even a feed row claiming the same id must not overwrite it
    impostor = {**request_rows[0], "Request ID": rid, "Status": "Denied", "Item Name": "Something else"}
    s = run_sync(conn, [], request_rows + [impostor])
    assert s["requests_added"] == 0
    rows = conn.execute('SELECT "Status", "Item Name" FROM dash_requests WHERE "Request ID" = %s', (rid,)).fetchall()
    assert rows == [("Shipped", "Gauze")]


def test_dashboard_quantity_survives_sync_until_sheet_changes(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    item_id = conn.execute("SELECT id FROM dash_items WHERE sheet_row = 2").fetchone()[0]
    conn.execute("SELECT * FROM set_item_quantity(%s, 5)", (item_id,))
    conn.commit()
    rows = copy.deepcopy(sheet_rows)
    rows[0][1]["Notes"] = "edited note"  # row edited, but not its Quantity cell
    s = run_sync(conn, rows, [])
    assert s["items_updated"] == 1
    assert conn.execute("SELECT quantity FROM dash_items WHERE id = %s", (item_id,)).fetchone()[0] == 5
    assert conn.execute('SELECT "Quantity" FROM dash_supplies WHERE "_sheet_row" = 2').fetchone()[0] == "5"
    rows[0][1]["Quantity"] = "12"  # now the sheet cell changes -> sheet wins
    run_sync(conn, rows, [])
    assert conn.execute("SELECT quantity FROM dash_items WHERE id = %s", (item_id,)).fetchone()[0] == 12
    assert conn.execute('SELECT "Quantity" FROM dash_supplies WHERE "_sheet_row" = 2').fetchone()[0] == "12"


def test_old_sheet_layout_then_new_sheet(conn, monkeypatch):
    """Sept snapshot (old layout: 'ER #1' box labels) then the Oct sheet, which
    replaced every row: all old rows end up removed, none duplicated."""
    old = load_old_supplies()
    s1 = run_sync(conn, old, load_requests("2026-09-06"))
    assert s1["items_added"] + s1["rows_skipped"] == len(old) == 1384
    assert count(conn, "SELECT count(*) FROM dash_items WHERE category_box = 'ER' AND box_number = 1") > 0
    assert count(conn, "SELECT count(*) FROM dash_items WHERE category NOT IN (SELECT name FROM category)") == 0
    from conftest import load_sheet_csv
    new = load_sheet_csv()
    refused = run_sync(conn, new, load_requests())  # 0% overlap: looks like the wrong sheet
    assert (refused["items_added"], refused["items_removed"]) == (0, 0)
    assert any("NOTHING was changed" in w for w in refused["warnings"])
    sept_ids = {r["Request ID"] for r in load_requests("2026-09-06")}
    oct_ids = {r["Request ID"] for r in load_requests()}
    assert refused["requests_added"] == len(oct_ids - sept_ids)  # requests don't depend on the sheet
    monkeypatch.setenv("B4P_MIN_KEY_OVERLAP", "0")  # "yes, this sheet replaced the old one"
    blocked = run_sync(conn, new, load_requests())
    assert blocked["items_removed"] == 0 and blocked["items_added"] == 1136  # removal guard still holds
    assert any("NOTHING was marked removed" in w for w in blocked["warnings"])
    monkeypatch.setenv("B4P_MAX_REMOVE_FRACTION", "1")  # "and the deletion is real"
    s2 = run_sync(conn, new, load_requests())
    assert s2["items_added"] == 0
    assert s2["items_removed"] == s1["items_added"]
    assert count(conn, "SELECT count(*) FROM dash_items") == 1136
    s3 = run_sync(conn, new, load_requests())
    assert (s3["items_added"], s3["items_updated"], s3["items_removed"], s3["lines_added"]) == (0, 0, 0, 0)


def test_unparseable_feed_rows_are_kept_and_flagged(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    # real rows with blank / 'Unknown' names are kept, flagged for review, and not in the catalog
    n = count(conn, "SELECT count(*) FROM inventory_item WHERE raw_name IN ('', 'Unknown')")
    assert n == 80
    assert count(conn, "SELECT count(*) FROM inventory_item WHERE raw_name IN ('', 'Unknown') "
                       "AND flagged AND supply_id IS NULL") == n
    assert count(conn, "SELECT count(*) FROM dash_items WHERE name LIKE 'Item %'") == 27


def test_missing_feed_records_error(conn, monkeypatch):
    from b4p import sources

    def boom(*a, **kw):
        raise sources.FeedError("down")
    monkeypatch.setattr(sources, "fetch_inventory", boom)
    monkeypatch.setattr(sources, "fetch_requests", boom)
    s = run_sync(conn)
    assert len(s["errors"]) == 2
    assert conn.execute("SELECT error FROM sync_run").fetchone()[0].startswith("inventory feed")


def test_reviewed_row_returns_to_the_queue_when_it_gains_a_patient_identifier(conn):
    from b4p.sync import run_sync
    base = {"Name": "Gauze", "Quantity": "5", "Manufacturer Name": "BD", "Date of Expiration": "2027-01",
            "Pallet Number": "1", "Box Number": "1", "Subcategory": "Gloves", "Review": "Yes", "Image URL": ""}
    def rows(name):
        return [(i + 2, dict(base, Name=name if i == 0 else f"Gauze {i}")) for i in range(5)]
    run_sync(conn, rows("Gauze"), [])
    conn.execute("UPDATE inventory_item SET reviewed_at = now()")
    run_sync(conn, rows("Gauze"), [])       # unchanged: stays reviewed
    assert conn.execute("SELECT count(*) FROM v_review_queue").fetchone()[0] == 0
    run_sync(conn, rows("Gauze Pt. ID " "12345"), [])   # gains a new reason
    got = conn.execute("SELECT sheet_row, review_reason FROM v_review_queue").fetchall()
    assert [g[0] for g in got] == [2] and "patient identifier" in got[0][1]


def test_daily_days_in_warehouse_tick_is_not_a_change(conn):
    from b4p.sync import run_sync
    base = {"Name": "Gauze", "Quantity": "5", "Manufacturer Name": "BD", "Date of Expiration": "2030-01",
            "Pallet Number": "1", "Box Number": "1", "Subcategory": "Gloves", "Image URL": ""}
    def rows(days):
        return [(i + 2, dict(base, Name=f"Gauze {i}", **{"Days in Warehouse": str(days + i)})) for i in range(30)]
    assert run_sync(conn, rows(5), [])["items_added"] == 30
    assert run_sync(conn, rows(5), [])["items_updated"] == 0
    tick = run_sync(conn, rows(6), [])            # tomorrow's formula value: nobody touched the rows
    assert tick["items_updated"] == 0
    changed = rows(6)
    changed[3][1]["Days in Warehouse"] = "90"     # a real change to one row
    assert run_sync(conn, changed, [])["items_updated"] == 1
    assert run_sync(conn, changed, [])["items_updated"] == 0
    shown = conn.execute('SELECT "Days in Warehouse" FROM dash_supplies WHERE "_sheet_row" = 5').fetchone()[0]
    assert shown == "90"
