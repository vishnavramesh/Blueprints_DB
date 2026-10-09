"""Database-level rules: constraints, the ledger trigger, audit log, views and
the plpgsql functions from migration 001. Talks to Postgres directly (no
service). Each test runs in a transaction that is rolled back.

    python3 -m unittest -v constraints_test
"""
import unittest

import psycopg

import harness

DB = "b4p_test_constraints"


class DbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        harness.fresh_db(DB)
        cls.conn = psycopg.connect(dbname=DB)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.cur = self.conn.cursor()

    def tearDown(self):
        self.conn.rollback()

    # helpers ---------------------------------------------------------------
    def one(self, sql, *args):
        self.cur.execute(sql, args)
        return self.cur.fetchone()[0]

    def rejects(self, sql, *args):
        """sql must fail; the failure is contained in a savepoint."""
        with self.assertRaises(psycopg.errors.Error):
            with self.conn.transaction():
                self.cur.execute(sql, args)

    def supply(self, name, mfr=None, category=3, ref=None):
        mid = None
        if mfr:
            mid = self.one("INSERT INTO manufacturer (canonical_name) VALUES (%s) "
                           "ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name "
                           "RETURNING manufacturer_id", mfr)
        return self.one("INSERT INTO supply (manufacturer_id, category_id, name, ref_number) "
                        "VALUES (%s,%s,%s,%s) RETURNING supply_id", mid, category, name, ref)

    def item(self, supply_id, qty=10, exp=None, **cols):
        cols = {"supply_id": supply_id, "quantity": qty, "expiration_date": exp, **cols}
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        return self.one(f"INSERT INTO inventory_item ({names}) VALUES ({marks}) RETURNING item_id",
                        *cols.values())

    def shipment(self, rid="REQ-T", status="approved", lines=()):
        rq = self.one("INSERT INTO requester (org_name) VALUES ('Clinic') "
                      "ON CONFLICT (org_name) DO UPDATE SET org_name = EXCLUDED.org_name "
                      "RETURNING requester_id")
        sid = self.one("INSERT INTO shipment (requester_id, request_id, status) VALUES (%s,%s,%s) "
                       "RETURNING shipment_id", rq, rid, status)
        for n, (name, qty) in enumerate(lines, 1):
            self.cur.execute("INSERT INTO shipment_item (shipment_id, line_no, item_name, quantity_requested) "
                             "VALUES (%s,%s,%s,%s)", (sid, n, name, qty))
        return sid


class ConstraintTests(DbTest):
    def test_negative_quantity_rejected(self):
        s = self.supply("Gauze 4x4")
        self.rejects("INSERT INTO inventory_item (supply_id, quantity) VALUES (%s, -1)", s)

    def test_quantity_upper_bound(self):
        s = self.supply("Gauze 4x4")
        self.rejects("INSERT INTO inventory_item (supply_id, quantity) VALUES (%s, 1000000)", s)

    def test_unmatched_item_must_be_flagged_with_raw_name(self):
        self.rejects("INSERT INTO inventory_item (supply_id, quantity) VALUES (NULL, 1)")
        self.rejects("INSERT INTO inventory_item (supply_id, flagged) VALUES (NULL, true)")
        self.item(None, flagged=True, raw_name="{scissors}")  # allowed

    def test_ref_unique_per_manufacturer(self):
        self.supply("Syringe 10ml", mfr="BD", ref="302995")
        self.rejects("INSERT INTO supply (manufacturer_id, category_id, name, ref_number) "
                     "SELECT manufacturer_id, 4, 'Other name', '302995' FROM manufacturer "
                     "WHERE canonical_name = 'BD'")
        self.supply("Syringe 10ml", mfr="Medline", ref="302995")  # other mfr: fine

    def test_same_name_same_null_manufacturer_rejected(self):
        self.supply("Kodel Knee Sling")
        self.rejects("INSERT INTO supply (category_id, name) VALUES (3, 'Kodel Knee Sling')")

    def test_box_number_positive_and_type_known(self):
        self.rejects("INSERT INTO box (box_type, box_number) VALUES ('ER', 0)")
        self.rejects("INSERT INTO box (box_type, box_number) VALUES ('NOPE', 1)")

    def test_shipment_status_enum(self):
        self.rejects("INSERT INTO shipment (status) VALUES ('lost')")

    def test_migration_recorded(self):
        self.assertEqual(self.one("SELECT count(*) FROM schema_migration WHERE version = '001'"), 1)


class LedgerTests(DbTest):
    def test_transactions_move_quantity(self):
        i = self.item(self.supply("Gauze 4x4"), qty=0)
        for kind, delta in [("received", 10), ("shipped", -3), ("adjusted", 2)]:
            self.cur.execute("INSERT INTO inventory_transaction (item_id, txn_type, delta) "
                             "VALUES (%s,%s,%s)", (i, kind, delta))
        self.assertEqual(self.one("SELECT quantity FROM inventory_item WHERE item_id = %s", i), 9)

    def test_overdraw_rejected_and_rolled_back(self):
        i = self.item(self.supply("Gauze 4x4"), qty=5)
        self.rejects("INSERT INTO inventory_transaction (item_id, txn_type, delta) "
                     "VALUES (%s, 'shipped', -6)", i)
        self.assertEqual(self.one("SELECT quantity FROM inventory_item WHERE item_id = %s", i), 5)
        self.assertEqual(self.one("SELECT count(*) FROM inventory_transaction WHERE item_id = %s", i), 0)

    def test_zero_delta_and_unknown_type_rejected(self):
        i = self.item(self.supply("Gauze 4x4"))
        self.rejects("INSERT INTO inventory_transaction (item_id, txn_type, delta) VALUES (%s,'adjusted',0)", i)
        self.rejects("INSERT INTO inventory_transaction (item_id, txn_type, delta) VALUES (%s,'stolen',1)", i)


class AuditTests(DbTest):
    def test_changes_are_audited_with_source(self):
        self.cur.execute("SELECT set_config('b4p.source', 'unit-test', true)")
        i = self.item(self.supply("Gauze 4x4"), qty=3)
        self.cur.execute("UPDATE inventory_item SET lot_number = 'L2' WHERE item_id = %s", (i,))
        self.cur.execute("SELECT op, source, old_row->>'lot_number', new_row->>'lot_number' "
                         "FROM audit_log WHERE table_name = 'inventory_item' AND row_id = %s "
                         "ORDER BY audit_id", (i,))
        self.assertEqual(self.cur.fetchall(),
                         [("INSERT", "unit-test", None, None), ("UPDATE", "unit-test", None, "L2")])

    def test_noop_update_not_audited(self):
        i = self.item(self.supply("Gauze 4x4"))
        self.cur.execute("UPDATE inventory_item SET quantity = quantity WHERE item_id = %s", (i,))
        self.assertEqual(self.one("SELECT count(*) FROM audit_log WHERE row_id = %s "
                                  "AND table_name = 'inventory_item' AND op = 'UPDATE'", i), 0)


class ViewTests(DbTest):
    def test_removed_items_hidden(self):
        s = self.supply("Gauze 4x4")
        keep, gone = self.item(s, qty=2), self.item(s, qty=5)
        self.cur.execute("UPDATE inventory_item SET removed_at = now() WHERE item_id = %s", (gone,))
        self.cur.execute("SELECT item_id FROM v_inventory WHERE item_id IN (%s,%s)", (keep, gone))
        self.assertEqual([r[0] for r in self.cur.fetchall()], [keep])

    def test_kpi_value_falls_back_to_category_default(self):
        self.cur.execute("UPDATE category SET default_unit_value_usd = 2.50 WHERE category_id = 3")
        s = self.supply("Gauze 4x4", category=3)
        self.item(s, qty=4)
        self.assertEqual(float(self.one("SELECT total_value FROM v_kpis")), 10.0)

    def test_expiring_buckets(self):
        s = self.supply("Gauze 4x4")
        self.item(s, exp="2000-01-01")
        self.cur.execute("INSERT INTO inventory_item (supply_id, quantity, expiration_date) "
                         "VALUES (%s, 1, CURRENT_DATE + 10), (%s, 1, CURRENT_DATE + 60), "
                         "(%s, 1, CURRENT_DATE + 400)", (s, s, s))
        self.cur.execute("SELECT bucket FROM v_expiring ORDER BY expiration_date")
        self.assertEqual([r[0] for r in self.cur.fetchall()], ["expired", "30d", "90d"])

    def test_name_numbers(self):
        self.assertEqual(self.one("SELECT name_numbers('BD 10ml Syringe 23G x 1.5')"), "10 23 1.5")

    def test_duplicates_ignore_size_variants(self):
        a = self.supply("UNI-SOLVE® Adhesive Remover Wipes")
        b = self.supply("UNI-SOLVE Adhesive Remover Wipes")
        self.supply("BD 10ml Syringe Luer-Lok Tip")
        self.supply("BD 20ml Syringe Luer-Lok Tip")
        self.cur.execute("SELECT supply_a, supply_b FROM v_duplicate_supplies")
        self.assertEqual(self.cur.fetchall(), [(a, b)])

    def test_suggestions_prefer_matching_numbers(self):
        self.supply("Shiley Tracheostomy Tube 6.5 mm")
        want = self.supply("Shiley Tracheostomy Tube 7.5 mm")
        self.assertEqual(self.one("SELECT supply_id FROM suggest_supplies('Shiley Tracheostomy Tube 7.5 mm', 1)"),
                         want)


class FunctionTests(DbTest):
    def test_merge_supply_moves_stock(self):
        keep, dup = self.supply("Wipes"), self.supply("Wipes (dup)")
        i = self.item(dup)
        self.assertEqual(self.one("SELECT merge_supply(%s, %s)", keep, dup), 1)
        self.assertEqual(self.one("SELECT supply_id FROM inventory_item WHERE item_id = %s", i), keep)
        self.assertEqual(self.one("SELECT count(*) FROM supply WHERE supply_id = %s", dup), 0)

    def test_merge_supply_refuses_self_and_missing(self):
        s = self.supply("Wipes")
        self.rejects("SELECT merge_supply(%s, %s)", s, s)
        self.rejects("SELECT merge_supply(%s, 999999)", s)

    def test_reserve_first_expiring_first_skipping_expired(self):
        s = self.supply("Shiley Tracheostomy Tube 7.5 mm")
        expired = self.item(s, qty=5, exp="2000-01-01")
        later = self.item(s, qty=5, exp="2031-01-01")
        sooner = self.item(s, qty=5, exp="2029-01-01")
        sid = self.shipment(lines=[("Shiley Tracheostomy Tube 7.5 mm", 4)])
        self.cur.execute("SELECT item_id FROM reserve_shipment(%s)", (sid,))
        self.assertEqual([r[0] for r in self.cur.fetchall()], [sooner])
        self.cur.execute("SELECT item_id FROM inventory_item WHERE reserved_for = %s", (sid,))
        self.assertEqual([r[0] for r in self.cur.fetchall()], [sooner])
        self.assertNotIn(expired, [sooner, later])

    def test_reserve_covers_quantity_across_lots_and_is_idempotent(self):
        s = self.supply("Gauze 4x4")
        a, b, _ = self.item(s, qty=3, exp="2029-01-01"), self.item(s, qty=3, exp="2030-01-01"), \
            self.item(s, qty=3, exp="2031-01-01")
        sid = self.shipment(lines=[("Gauze 4x4", 5)])
        self.cur.execute("SELECT item_id FROM reserve_shipment(%s)", (sid,))
        self.assertEqual([r[0] for r in self.cur.fetchall()], [a, b])
        self.cur.execute("SELECT item_id FROM reserve_shipment(%s)", (sid,))
        self.assertEqual(self.cur.fetchall(), [])  # already covered
        self.assertEqual(self.one("SELECT release_shipment(%s)", sid), 2)

    def test_reserve_refuses_closed_shipment(self):
        sid = self.shipment(status="delivered", lines=[("Gauze", 1)])
        self.rejects("SELECT * FROM reserve_shipment(%s)", sid)

    def test_reserve_skips_unmatched_lines(self):
        self.item(self.supply("Gauze 4x4"))
        sid = self.shipment(lines=[("Completely unrelated widget", 1)])
        self.cur.execute("SELECT * FROM reserve_shipment(%s)", (sid,))
        self.assertEqual(self.cur.fetchall(), [])


if __name__ == "__main__":
    unittest.main()
