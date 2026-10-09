"""End-to-end: fake dashboard feed -> b4p-service sync -> Postgres -> HTTP API.

Each test gets a fresh database and its own service process, drives the feed,
runs POST /sync/run, and checks both the database and the API.

    python3 -m unittest -v scenario_test
    python3 -m unittest -v scenario_test.SyncTests.test_edit_updates_in_place
"""
import concurrent.futures
import time
import unittest

import harness
from harness import request_row, supply_row

DB = "b4p_test_scenarios"


class ServiceTest(unittest.TestCase):
    token = harness.TOKEN

    def setUp(self):
        harness.fresh_db(DB)
        self.feed = harness.FakeFeed()
        self.svc = harness.Service(DB, self.feed.url, token=self.token)
        self.db = harness.connect(DB)

    def tearDown(self):
        self.db.close()
        code = self.svc.stop()
        self.feed.close()
        self.assertEqual(code, 0, "service did not shut down cleanly:\n" +
                         self.svc.log_path.read_text()[-2000:])

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()[0]

    def active_items(self):
        return self.one("SELECT count(*) FROM inventory_item WHERE removed_at IS NULL")


class SyncTests(ServiceTest):
    def test_new_rows_inserted_once(self):
        self.feed.supplies = [supply_row(1), supply_row(2), supply_row(3, Quantity="1 (full box)")]
        run = self.svc.sync()
        self.assertEqual(run["items_added"], 3)
        self.assertEqual(self.svc.sync()["items_added"], 0)
        self.assertEqual(self.active_items(), 3)
        self.assertEqual(self.q("SELECT txn_type, delta, source FROM inventory_transaction ORDER BY txn_id"),
                         [("received", 10, "sync"), ("received", 10, "sync"), ("received", 1, "sync")])
        self.assertEqual(self.one("SELECT sum(quantity) FROM inventory_item"), 21)

    def test_edit_updates_in_place(self):
        """The old hash-as-identity sync inserted an edited row as a second item."""
        self.feed.supplies = [supply_row(1)]
        self.svc.sync()
        self.feed.supplies = [supply_row(1, Quantity="4", **{"Lot Number": "LOT1-fixed"})]
        run = self.svc.sync()
        self.assertEqual((run["items_added"], run["items_updated"]), (0, 1))
        self.assertEqual(self.q("SELECT quantity, lot_number FROM inventory_item"), [(4, "LOT1-fixed")])
        self.assertEqual(self.q("SELECT txn_type, delta FROM inventory_transaction ORDER BY txn_id"),
                         [("received", 10), ("adjusted", -6)])
        self.assertEqual(self.one("SELECT source FROM audit_log WHERE table_name = 'inventory_item' "
                                  "AND op = 'UPDATE' ORDER BY audit_id DESC LIMIT 1"), "sync")

    def test_removed_row_hidden_then_restored(self):
        self.feed.supplies = [supply_row(n) for n in range(1, 5)]
        self.svc.sync()
        self.feed.supplies = [supply_row(n) for n in range(1, 4)]
        self.assertEqual(self.svc.sync()["items_removed"], 1)
        names = [r["name"] for r in self.svc.get("/api/supplies")[1]]
        self.assertNotIn("Test Gauze Pad 4x4 in", names)
        self.assertEqual(len(names), 3)
        self.feed.supplies = [supply_row(n) for n in range(1, 5)]
        self.assertEqual(self.svc.sync()["items_updated"], 1)
        self.assertEqual(self.active_items(), 4)

    def test_truncated_feed_does_not_remove(self):
        self.feed.supplies = [supply_row(n) for n in range(1, 5)]
        self.svc.sync()
        self.feed.supplies = [supply_row(1)]  # 1 of 4: looks like a broken backend
        self.assertEqual(self.svc.sync()["items_removed"], 0)
        self.assertEqual(self.active_items(), 4)

    def test_multi_line_request_keeps_every_line(self):
        """The old sync stored only the first line of a multi-line request."""
        self.feed.requests = [request_row("REQ-1", "Gauze"), request_row("REQ-1", "Tape", qty="5"),
                              request_row("REQ-1", "Saline")]
        run = self.svc.sync()
        self.assertEqual((run["requests_added"], run["lines_added"]), (1, 3))
        self.feed.requests.append(request_row("REQ-1", "Gloves"))
        self.assertEqual(self.svc.sync()["lines_added"], 1)
        self.assertEqual([r[0] for r in self.q("SELECT item_name FROM shipment_item ORDER BY line_no")],
                         ["Gauze", "Tape", "Saline", "Gloves"])
        api = self.svc.get("/requests")[1]
        self.assertEqual([r["Item Name"] for r in api], ["Gauze", "Tape", "Saline", "Gloves"])
        self.assertEqual(api[1]["Quantity Requested"], "5")

    def test_status_change(self):
        self.feed.requests = [request_row("REQ-1", "Gauze")]
        self.svc.sync()
        self.feed.requests = [request_row("REQ-1", "Gauze", status="Approved")]
        self.assertEqual(self.svc.sync()["status_changes"], 1)
        self.assertEqual(self.one("SELECT status FROM shipment"), "approved")

    def test_bad_timestamp_does_not_block_sync(self):
        self.feed.requests = [request_row("REQ-1", "Gauze", Timestamp="yesterday-ish"),
                              request_row("REQ-2", "Tape")]
        self.feed.supplies = [supply_row(1)]
        run = self.svc.sync()
        self.assertEqual((run["requests_added"], run["items_added"], run["rows_skipped"]), (2, 1, 0))

    def test_row_postgres_rejects_is_skipped_not_fatal(self):
        # A test-only constraint makes Postgres refuse one specific row.
        self.db.execute("ALTER TABLE inventory_item ADD CONSTRAINT test_poison "
                        "CHECK (lot_number IS DISTINCT FROM 'POISON')")
        self.feed.supplies = [supply_row(1), supply_row(2, **{"Lot Number": "POISON"}),
                              supply_row(3)]
        run = self.svc.sync()
        self.assertEqual((run["items_added"], run["rows_skipped"]), (2, 1))
        self.assertIsNone(run["error"])
        # the rejected row's catalog entry was rolled back with it
        self.assertEqual(self.one("SELECT count(*) FROM supply"), 2)

    def test_nul_bytes_stripped(self):
        # legal JSON, but Postgres text can't hold NUL (libpq would truncate)
        self.feed.supplies = [supply_row(1, Name="bad\u0000name")]
        self.svc.sync()
        self.assertEqual(self.one("SELECT name FROM supply"), "badname")

    def test_one_feed_down_other_still_syncs(self):
        self.feed.supplies = [supply_row(1)]
        self.feed.requests = [request_row("REQ-1", "Gauze")]
        self.feed.status["/api/supplies"] = 500
        run = self.svc.sync()
        self.assertEqual((run["supplies_feed_ok"], run["requests_feed_ok"]), (False, True))
        self.assertEqual((run["items_added"], run["requests_added"]), (0, 1))
        self.feed.status.clear()
        self.feed.raw["/requests"] = b"<html>502 Bad Gateway</html>"
        run = self.svc.sync()
        self.assertEqual((run["supplies_feed_ok"], run["requests_feed_ok"]), (True, False))
        self.assertEqual(run["items_added"], 1)

    def test_unreadable_scan_stays_out_of_catalog(self):
        self.feed.supplies = [supply_row(1, Name="{surgical scissors}")]
        self.svc.sync()
        self.assertEqual(self.q("SELECT supply_id, flagged, raw_name FROM inventory_item"),
                         [(None, True, "{surgical scissors}")])
        self.assertEqual(self.one("SELECT count(*) FROM supply"), 0)
        queue = self.svc.get("/review/queue")[1]
        self.assertEqual(queue["total"], 1)
        self.assertEqual(queue["items"][0]["match_text"], "surgical scissors")

    def test_rules_come_from_database(self):
        self.feed.supplies = [supply_row(1, **{"Box Number": "APP-PPE #1"})]
        self.svc.sync()
        self.assertIsNone(self.one("SELECT box_id FROM inventory_item"))
        self.db.execute("INSERT INTO box_type_alias (alias, code) VALUES ('APP-PPE', 'PH/PPE')")
        self.feed.supplies = [supply_row(1, **{"Box Number": "APP-PPE #1", "Notes": "edited"})]
        self.svc.sync()
        self.assertEqual(self.one("SELECT box_type || '-' || box_number FROM box"), "PH/PPE-1")

    def test_warnings_recorded(self):
        self.feed.supplies = [supply_row(1, **{"Date of Expiration": "1/1/2001"})]
        run = self.svc.sync()
        self.assertEqual(run["warnings"]["expired stock not yet flagged or reserved"], 1)


class ApiTests(ServiceTest):
    def seed(self):
        self.feed.supplies = [
            supply_row(1),
            supply_row(2, Quantity="3", **{"Date of Expiration": "1/1/2001"}),
            supply_row(3, Name="Shiley Tracheostomy Tube 7.5 mm", Category="Airway & Oxygen",
                       **{"Box Number": "TC/RC 11", "Date of Expiration": "1/1/2029"}),
            supply_row(4, Name="Shiley Tracheostomy Tube 7.5 mm", Category="Airway & Oxygen",
                       **{"Box Number": "TC/RC 12", "Date of Expiration": "1/1/2031"}),
            supply_row(5, Name="{scissors}", Review="Review needed"),
        ]
        self.feed.requests = [request_row("REQ-9", "Shiley Tracheostomy Tube 7.5 mm", qty="2",
                                          status="Approved")]
        self.svc.sync()

    def test_unfiltered_supplies_keep_dashboard_shape(self):
        self.seed()
        rows = self.svc.get("/api/supplies")[1]
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["quantity"], "10")  # strings, like the Render backend
        self.assertIn("reserved_for_org", rows[0])

    def test_supply_filters(self):
        self.seed()
        get = lambda qs: self.svc.get("/api/supplies?" + qs)[1]
        self.assertEqual(len(get("category=airway%20%26%20oxygen")), 2)
        self.assertEqual(len(get("category=6")), 2)
        self.assertEqual(len(get("box=tc/rc-11")), 1)
        self.assertEqual(len(get("q=shiley")), 2)
        self.assertEqual(len(get("flagged=true")), 1)
        self.assertEqual(len(get("expiring_within=30")), 1)
        self.assertEqual([r["item_id"] for r in get("limit=2&offset=1")],
                         [r["item_id"] for r in get("limit=5")][1:3])

    def test_bad_params_are_400(self):
        for qs in ["limit=0", "limit=abc", "flagged=maybe", "expiring_within=-1", "offset=1.5"]:
            code, body = self.svc.get("/api/supplies?" + qs)
            self.assertEqual(code, 400, qs)
            self.assertEqual(body["error"]["code"], "bad_request")

    def test_expiring_alerts(self):
        self.seed()
        body = self.svc.get("/alerts/expiring?days=90")[1]
        self.assertEqual(body["summary"], {"expired": 1, "30d": 0, "90d": 0})
        self.assertEqual(body["items"][0]["quantity"], 3)

    def test_review_link_and_new_supply(self):
        self.seed()
        item = self.svc.get("/review/queue")[1]["items"][0]["item_id"]
        code, body = self.svc.post(f"/review/{item}/new-supply",
                                   {"name": "Mayo Scissors 6.75 in", "category_id": 2,
                                    "manufacturer": "Medline"})
        self.assertEqual(code, 201, body)
        self.assertEqual(self.svc.get("/review/queue")[1]["total"], 0)
        # A later dashboard edit must not undo the human decision.
        self.feed.supplies[4] = supply_row(5, Name="{scissors}", Review="Review needed", Notes="edit")
        self.svc.sync()
        self.assertEqual(self.q("SELECT flagged, supply_id IS NOT NULL, notes FROM inventory_item "
                                "WHERE item_id = %s", item), [(False, True, "edit")])
        self.assertEqual(self.svc.post(f"/review/{item}/link", {"supply_id": 999999})[0], 404)
        self.assertEqual(self.svc.post("/review/abc/link", {"supply_id": 1})[0], 400)

    def test_reserve_picklist_release(self):
        self.seed()
        code, body = self.svc.post("/shipments/REQ-9/reserve")
        self.assertEqual(code, 200, body)
        self.assertEqual(len(body["reserved"]), 1)  # the 2029 lot (qty 10) covers 2
        pick = self.svc.get("/shipments/REQ-9/picklist")[1]
        self.assertEqual([(p["box_label"], p["expiration_date"]) for p in pick],
                         [("TC/RC-11", "2029-01-01")])
        avail = {a["item_id"]: a for a in self.svc.get("/inventory/availability")[1]}
        self.assertEqual(avail[pick[0]["item_id"]]["availability_status"], "Requested")
        self.assertEqual(self.svc.post("/shipments/REQ-9/release")[1], {"released": 1})
        self.assertEqual(self.svc.post("/shipments/REQ-404/reserve")[0], 404)

    def test_reserve_closed_shipment_is_409(self):
        self.seed()
        self.db.execute("UPDATE shipment SET status = 'delivered'")
        code, body = self.svc.post("/shipments/REQ-9/reserve")
        self.assertEqual(code, 409)
        self.assertIn("closed", body["error"]["message"])

    def test_duplicates_and_merge(self):
        self.seed()
        a = self.one("INSERT INTO supply (category_id, name) VALUES (3, 'UNI-SOLVE® Adhesive Remover Wipes') "
                     "RETURNING supply_id")
        b = self.one("INSERT INTO supply (category_id, name) VALUES (3, 'UNI-SOLVE Adhesive Remover Wipes') "
                     "RETURNING supply_id")
        dups = self.svc.get("/catalog/duplicates")[1]
        self.assertIn((a, b), [(d["supply_a"], d["supply_b"]) for d in dups])
        self.assertEqual(self.svc.post("/catalog/merge", {"keep": a, "drop": b})[0], 200)
        self.assertEqual(self.svc.post("/catalog/merge", {"keep": a, "drop": a})[0], 409)

    def test_boxes(self):
        self.seed()
        boxes = {b["label"]: b for b in self.svc.get("/boxes")[1]}
        self.assertEqual(boxes["TC/RC-11"]["units"], 10)
        contents = self.svc.get("/boxes/contents?label=TC/RC-11")[1]
        self.assertEqual(contents[0]["name"], "Shiley Tracheostomy Tube 7.5 mm")

    def test_writes_need_token(self):
        self.seed()
        code, body = self.svc.post("/sync/run", token=False)
        self.assertEqual(code, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(self.svc.post("/shipments/REQ-9/reserve", token=False)[0], 401)

    def test_kpis_and_health(self):
        self.seed()
        self.assertEqual(self.svc.get("/kpis")[1]["total_entries"], 5)
        self.assertEqual(self.svc.get("/health")[1]["inventory_items"], 5)


class ConcurrencyTests(ServiceTest):
    def test_parallel_reads_during_sync(self):
        """Hammer the read endpoints from 16 threads while syncs run: no
        errors, and the pool never deadlocks."""
        self.feed.supplies = [supply_row(n) for n in range(1, 301)]
        paths = ["/api/supplies", "/requests", "/inventory/availability", "/health",
                 "/kpis", "/review/queue", "/alerts/expiring", "/boxes"]
        stop = time.time() + 4
        errors, done = [], [0]

        def worker(i):
            n = 0
            while time.time() < stop:
                code, body = self.svc.get(paths[(i + n) % len(paths)])
                if code != 200:
                    errors.append((code, body))
                n += 1
            return n

        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            futs = [pool.submit(worker, i) for i in range(16)]
            for k in range(3):
                self.feed.supplies = [supply_row(n, Quantity=str(10 + k)) for n in range(1, 301)]
                self.svc.sync()
            done[0] = sum(f.result() for f in futs)
        self.assertEqual(errors[:3], [])
        self.assertGreater(done[0], 100)
        self.assertEqual(self.one("SELECT sum(quantity) FROM inventory_item"), 300 * 12)


if __name__ == "__main__":
    unittest.main()
