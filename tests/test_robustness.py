"""Regression tests for the debugger's attacks (scratchpad/debugger t01-t08, t13)."""
import copy
import threading

import psycopg
import pytest

from b4p import db
from b4p import normalize as N
from b4p.sync import run_sync

NEW_REQUEST = [{"Request ID": "REQ-1-001", "Org Name": "Clinic", "Org Email": "c@example.org",
                "Item Name": "Gauze", "Category": "", "Quantity Requested": "2", "Status": "Under Review",
                "Timestamp": "", "Review Flag": "FALSE"}]


def one(conn, sql, *args):
    return conn.execute(sql, args or None).fetchone()[0]


# --- t01: one poison cell must not abort the sync --------------------------
@pytest.mark.parametrize("col,val", [
    ("Box Number", "12345678901"), ("Box Number", "ER 99999999999"), ("Pallet Number", "99999999999"),
    ("Days in Warehouse", "800000"), ("Days in Warehouse", "10000000000"),
    ("Price per unit", "$123456789"), ("Name", "Gauze\x00pad"), ("Notes", "x\x00"),
    ("Image URL", "u\x00rl"), ("Quantity", "-5"), ("Quantity", "99999999999999999999"),
])
def test_poison_cell_is_contained(conn, sheet_rows, col, val):
    rows = copy.deepcopy(sheet_rows)
    rows[10][1][col] = val
    s = run_sync(conn, rows, NEW_REQUEST)
    assert not s["errors"]
    assert s["items_added"] == 1136
    assert s["requests_added"] == 1  # the rest of the run still happened
    item = conn.execute("SELECT raw_name, notes, quantity, box_id FROM inventory_item WHERE sheet_row = 12").fetchone()
    assert item is not None and "\x00" not in (item[0] or "") + (item[1] or "")
    assert 0 <= item[2] <= 999_999


def test_failing_row_is_skipped_with_warning(conn, sheet_rows, monkeypatch):
    real = N.read_inventory_row

    def flaky(row, *a, **k):
        if row.get("Name") == sheet_rows[10][1]["Name"]:
            raise ValueError("boom")
        return real(row, *a, **k)
    monkeypatch.setattr(N, "read_inventory_row", flaky)
    s = run_sync(conn, sheet_rows, NEW_REQUEST)
    assert not s["errors"] and s["requests_added"] == 1
    assert any("skipped (ValueError: boom)" in w for w in s["warnings"])
    assert s["items_added"] == 1136 - sum(1 for _, r in sheet_rows if r.get("Name") == sheet_rows[10][1]["Name"])


def test_failing_db_row_rolls_back_only_that_row(conn, sheet_rows, monkeypatch):
    """A database error in one row (here: an invalid category id) is contained by its savepoint,
    including get-or-create rows made for it."""
    from b4p import sync
    real = sync._Ids.category

    def bad(self, name):
        return 999 if name == "Sutures" else real(self, name)
    monkeypatch.setattr(sync._Ids, "category", bad)
    s = run_sync(conn, sheet_rows, [])
    sutures = sum(1 for _, r in sheet_rows if r.get("Subcategory") == "Sutures")
    assert not s["errors"] and s["items_added"] == 1136 - sutures and s["rows_skipped"] == 1 + sutures


def test_crashed_run_is_still_recorded(conn, sheet_rows, monkeypatch):
    from b4p import sync

    def explode(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(sync, "sync_requests", explode)
    s = run_sync(conn, sheet_rows, NEW_REQUEST)
    assert s["errors"] and "disk on fire" in s["errors"][0]
    assert one(conn, "SELECT count(*) FROM inventory_item") == 0  # everything rolled back
    err = one(conn, "SELECT error FROM sync_run ORDER BY run_id DESC LIMIT 1")
    assert "rolled back" in err and "disk on fire" in err


# --- t02: renamed headers stop the sync before it writes -------------------
@pytest.mark.parametrize("old,new", [("Quantity", "Qty"), ("Image URL", "Photo"), ("Name", "Item Name"),
                                     ("Box Number", "Box"), ("Date of Expiration", "Expiry")])
def test_renamed_header_changes_nothing(conn, sheet_rows, old, new):
    run_sync(conn, sheet_rows, [])
    item = one(conn, "SELECT id FROM dash_items WHERE sheet_row = 2")
    conn.execute("SELECT * FROM set_item_quantity(%s, 42)", (item,))
    conn.commit()
    renamed = [(n, {(new if k == old else k): v for k, v in r.items()}) for n, r in sheet_rows]
    s = run_sync(conn, renamed, NEW_REQUEST)
    assert any(old in e and "missing required column" in e for e in s["errors"])
    assert (s["items_added"], s["items_updated"], s["items_removed"]) == (0, 0, 0)
    assert one(conn, "SELECT quantity FROM dash_items WHERE id = %s", item) == 42
    assert one(conn, "SELECT count(*) FROM dash_items") == 1136
    assert s["requests_added"] == 1  # requests are independent of the sheet
    assert one(conn, "SELECT error FROM sync_run ORDER BY run_id DESC LIMIT 1").startswith("inventory:")


def test_empty_feed_changes_nothing(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    s = run_sync(conn, [], [])
    assert s["errors"] and one(conn, "SELECT count(*) FROM dash_items") == 1136


# --- t03: duplicate Image URL ---------------------------------------------
def test_duplicate_url_deleting_one_copy_keeps_identities(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    original = one(conn, "SELECT id FROM dash_items WHERE sheet_row = 2")
    rows = copy.deepcopy(sheet_rows)
    dup = dict(rows[0][1], **{"Box Number": "99", "Pallet Number": "9"})
    rows.append((rows[-1][0] + 1, dup))
    s = run_sync(conn, rows, [])
    assert (s["items_added"], s["items_updated"]) == (1, 0)  # the original keeps its id untouched
    assert any("share the same identity" in w for w in s["warnings"])
    copy_id = one(conn, "SELECT id FROM dash_items WHERE box_number = 99")
    conn.execute("SELECT * FROM set_item_quantity(%s, 0)", (original,))  # original lot used up
    conn.commit()
    s = run_sync(conn, rows[1:], [])  # ...and its row deleted from the sheet
    assert s["items_removed"] == 1
    assert one(conn, "SELECT removed_at IS NOT NULL FROM inventory_item WHERE item_id::text = %s", original)
    assert one(conn, "SELECT quantity FROM dash_items WHERE id = %s", copy_id) == 16  # copy unaffected
    again = run_sync(conn, rows[1:], [])
    assert (again["items_added"], again["items_updated"], again["items_removed"]) == (0, 0, 0)


def test_duplicate_urls_from_the_start(conn, sheet_rows):
    rows = copy.deepcopy(sheet_rows)
    rows.append((rows[-1][0] + 1, dict(rows[0][1], **{"Box Number": "99"})))
    rows.append((rows[-1][0] + 1, dict(rows[0][1])))  # an exact copy too
    s = run_sync(conn, rows, [])
    assert s["items_added"] == 1138 and not s["errors"]
    again = run_sync(conn, rows, [])
    assert (again["items_added"], again["items_updated"], again["items_removed"]) == (0, 0, 0)
    first = one(conn, "SELECT id FROM dash_items WHERE box_number = 99")
    s = run_sync(conn, [r for r in rows if r[1].get("Box Number") != "99"], [])
    assert s["items_removed"] == 1 and one(conn, "SELECT count(*) FROM dash_items WHERE id = %s", first) == 0


# --- t04: removal guard ----------------------------------------------------
def test_truncated_feed_removes_nothing(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    s = run_sync(conn, sheet_rows[: int(len(sheet_rows) * 0.51)], [])
    assert s["items_removed"] == 0 and any("NOTHING was marked removed" in w for w in s["warnings"])
    assert one(conn, "SELECT count(*) FROM dash_items") == 1136
    stored = one(conn, "SELECT warnings::text FROM sync_run ORDER BY run_id DESC LIMIT 1")
    assert "B4P_MAX_REMOVE_FRACTION" in stored


def test_padded_junk_feed_removes_nothing(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    headers = dict.fromkeys(sheet_rows[0][1], "")
    junk = [(i, {**headers, "Name": "x", "Image URL": f"junk{i}"}) for i in range(2, 2000)]
    s = run_sync(conn, junk, [])
    assert s["items_removed"] == 0
    assert one(conn, "SELECT count(*) FROM inventory_item WHERE removed_at IS NULL "
                     "AND source_key NOT LIKE 'junk%%'") == 1136


def test_removal_within_limit_and_override(conn, sheet_rows, monkeypatch):
    run_sync(conn, sheet_rows, [])
    keep = sheet_rows[: len(sheet_rows) - 150]  # ~13% removed: allowed
    assert run_sync(conn, keep, [])["items_removed"] == 150
    assert run_sync(conn, keep[:600], [])["items_removed"] == 0  # ~38%: refused
    monkeypatch.setenv("B4P_MAX_REMOVE_FRACTION", "0.5")
    blank_after_600 = sum(1 for _, r in keep[600:] if not any(v.strip() for v in r.values()))
    assert run_sync(conn, keep[:600], [])["items_removed"] == len(keep) - 600 - blank_after_600


# --- t06: sync vs set_item_quantity race -----------------------------------
def test_dashboard_write_during_sync_wins(conn, test_db, sheet_rows, monkeypatch):
    run_sync(conn, sheet_rows, [])
    target = one(conn, "SELECT id FROM dash_items ORDER BY sheet_row DESC LIMIT 1")
    shifted = [(n + 1, r) for n, r in sheet_rows]  # a row inserted at the top: every row "changes"
    real, fired = N.read_inventory_row, []

    def during_sync(*a, **k):
        if not fired:  # the sync has read the table but not yet written the target row
            fired.append(1)
            with db.connect(test_db, autocommit=True) as other:
                other.execute("SELECT * FROM set_item_quantity(%s, 3)", (target,))
        return real(*a, **k)
    monkeypatch.setattr(N, "read_inventory_row", during_sync)
    s = run_sync(conn, shifted, [])
    assert s["items_updated"] == 1136 and fired
    assert one(conn, "SELECT quantity FROM dash_items WHERE id = %s", target) == 3


# --- t07: ids, emails, request ids ------------------------------------------
def test_blank_email_does_not_inherit_someone_elses(conn):
    conn.execute("""SELECT create_request('Shared Org', 'attacker@evil.test', '[{"item_name":"bait","quantity":1}]')""")
    rid = one(conn, """SELECT create_request('shared org', '   ', '[{"item_name":"real need","quantity":9}]')""")
    got = conn.execute('SELECT "Request ID" FROM dash_requests WHERE lower("Org Email") = %s',
                       ("attacker@evil.test",)).fetchall()
    assert rid not in [g[0] for g in got] and len(got) == 1
    assert one(conn, 'SELECT "Org Email" FROM dash_requests WHERE "Request ID" = %s', rid) == ""


def test_request_id_whitespace_is_normalized(conn):
    items = psycopg.types.json.Jsonb([{"item_name": "a", "quantity": 1}])
    assert one(conn, "SELECT create_request('Imp', 'i@x', %s, '  REQ-1-001 ')", items) == "REQ-1-001"
    assert one(conn, "SELECT create_request('Imp', 'i@x', %s, 'REQ-1-001')", items) == "REQ-1-001"
    assert one(conn, "SELECT count(*) FROM shipment") == 1
    assert one(conn, "SELECT set_request_status(' REQ-1-001 ', 'Approved')") is True


# --- t08: concurrent create_request never collides --------------------------
def test_concurrent_create_request(conn, test_db):
    errors, ok = [], []

    def worker():
        with db.connect(test_db, autocommit=True) as c:
            for _ in range(60):
                try:
                    ok.append(one(c, """SELECT create_request('race', 'r@x', '[{"item_name":"a","quantity":1}]')"""))
                except psycopg.Error as e:
                    errors.append(type(e).__name__)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and len(set(ok)) == len(ok) == 480


# --- t13: concurrent syncs + malformed request feed ------------------------
def test_concurrent_syncs_serialize(conn, test_db, sheet_rows):
    reqs = [{"Request ID": f"REQ-9-{i:03d}", "Org Name": f"Org{i % 3}", "Org Email": "o@x", "Item Name": "a",
             "Quantity Requested": "1", "Status": "Approved", "Timestamp": ""} for i in range(50)]
    results = []

    def go():
        with db.connect(test_db) as c:
            results.append(run_sync(c, sheet_rows, reqs))
    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r["items_added"] for r in results) == [0, 0, 0, 1136]
    assert all(not r["errors"] for r in results)
    assert one(conn, "SELECT count(*) FROM shipment") == 50


@pytest.mark.parametrize("feed", [[None], ["oops", 3], [{"Request ID": None, "Org Name": None}],
                                  [{"Request ID": 5, "Org Name": ["x"], "Item Name": {"a": 1},
                                    "Quantity Requested": None}]])
def test_malformed_request_feed_does_not_crash(conn, sheet_rows, feed):
    s = run_sync(conn, sheet_rows, feed)
    assert not s["errors"] and s["items_added"] == 1136


# --- round 2 ----------------------------------------------------------------
def test_n5_slowly_shrinking_feed_is_capped_over_24h(conn, sheet_rows):
    """14% less every run: only the first run's removal passes (rolling 24h baseline)."""
    run_sync(conn, sheet_rows, [])
    cur, removed = sheet_rows, []
    for _ in range(5):
        cur = cur[: int(len(cur) * 0.86)]
        removed.append(run_sync(conn, cur, [])["items_removed"])
    assert removed[0] > 0 and removed[1:] == [0, 0, 0, 0]
    assert one(conn, "SELECT count(*) FROM dash_items") >= 0.85 * 1136
    assert "last 24h" in one(conn, "SELECT warnings::text FROM sync_run ORDER BY run_id DESC LIMIT 1")


def test_n5_wrong_tab_with_same_headers_changes_nothing(conn, sheet_rows):
    run_sync(conn, sheet_rows, [])
    other = [(i + 2, {**d, "Image URL": f"https://drive.google.com/file/d/OTHER{i}/view",
                      "Name": f"other tab item {i}"}) for i, (_, d) in enumerate(sheet_rows[:341])]
    s = run_sync(conn, other, NEW_REQUEST)
    assert (s["items_added"], s["items_updated"], s["items_removed"]) == (0, 0, 0)
    assert any("wrong tab" in w for w in s["warnings"])
    assert s["requests_added"] == 1
    assert one(conn, "SELECT count(*) FROM inventory_item") == 1136


def test_n5_guards_do_not_block_a_first_load(conn, sheet_rows):
    small = sheet_rows[:10]
    assert run_sync(conn, small, [])["items_added"] == 10
    s = run_sync(conn, sheet_rows, [])  # 10 known out of 1136: fine, database is still tiny
    assert s["items_added"] == 1126


def _with_copy(sheet_rows):
    rows = copy.deepcopy(sheet_rows)
    rows.append((rows[-1][0] + 1, dict(rows[0][1], **{"Box Number": "99"})))
    return rows


def test_n6_editing_a_copy_keeps_its_id_and_quantity(conn, sheet_rows):
    rows = _with_copy(sheet_rows)
    run_sync(conn, rows, [])
    copy_id = one(conn, "SELECT id FROM dash_items WHERE box_number = 99")
    owner_id = one(conn, "SELECT id FROM dash_items WHERE sheet_row = 2")
    conn.execute("SELECT * FROM set_item_quantity(%s, 5)", (copy_id,))
    conn.commit()
    rows[-1][1]["Notes"] = "recounted"  # edit the copy
    s = run_sync(conn, rows, [])
    assert (s["items_added"], s["items_updated"], s["items_removed"]) == (0, 1, 0)
    assert one(conn, "SELECT quantity FROM dash_items WHERE id = %s", copy_id) == 5
    rows[0][1]["Notes"] = "owner edited"  # edit the original too
    s = run_sync(conn, rows, [])
    assert (s["items_added"], s["items_updated"], s["items_removed"]) == (0, 1, 0)
    assert one(conn, "SELECT box_number FROM dash_items WHERE id = %s", owner_id) == 4
    assert one(conn, "SELECT box_number FROM dash_items WHERE id = %s", copy_id) == 99
    rows[-1][1]["Box Number"] = "98"  # even editing the cell that told them apart
    s = run_sync(conn, rows, [])
    assert (s["items_added"], s["items_updated"], s["items_removed"]) == (0, 1, 0)
    assert one(conn, "SELECT box_number FROM dash_items WHERE id = %s", copy_id) == 98
    again = run_sync(conn, rows, [])
    assert (again["items_added"], again["items_updated"], again["items_removed"]) == (0, 0, 0)


def test_n7_lone_surrogate_does_not_sink_the_run(conn, sheet_rows):
    from b4p.sources import values_to_rows
    import json
    grid = json.loads('[["Name","Quantity","Image URL","Manufacturer Name","Date of Expiration","Pallet Number",'
                      '"Box Number","Subcategory"],["Gauze \\ud83d","3","https://x.test/1","BD","","1","2","Gloves"]]')
    rows = sheet_rows + [(r + 2000, d) for r, d in values_to_rows(grid)]
    rows[3] = (rows[3][0], {**rows[3][1], "Name": "\ud800 lone surrogate"})  # straight into run_sync too
    s = run_sync(conn, rows, NEW_REQUEST)
    assert not s["errors"] and s["items_added"] == 1137 and s["requests_added"] == 1
    assert one(conn, "SELECT raw_name FROM inventory_item WHERE image_url = 'https://x.test/1'") == "Gauze �"


def test_n13_unreadable_expiration_is_flagged(conn, sheet_rows):
    rows = copy.deepcopy(sheet_rows)
    rows[0][1]["Date of Expiration"] = "02/30/2026"
    rows[1][1]["Date of Expiration"] = "EXP 2027-03"
    s = run_sync(conn, rows, [])
    assert any("unreadable Date of Expiration '02/30/2026'" in w for w in s["warnings"])
    assert conn.execute("SELECT d.expiration_date, i.flagged FROM dash_items d JOIN inventory_item i "
                        "ON i.item_id::text = d.id WHERE d.sheet_row = 2").fetchone() == (None, True)
    assert one(conn, "SELECT expiration_date FROM dash_items WHERE sheet_row = 3") == "2027-03-31"
    assert one(conn, "SELECT count(*) FROM v_review_queue WHERE sheet_row = 2") == 1
