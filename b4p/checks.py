"""Consistency checks (what monitor.py used to warn about).

Each check is (label, SQL returning one number). Non-zero = worth a look.
"""
from __future__ import annotations

CHECKS = [
    ("expired stock still on the shelf",
     "SELECT count(*) FROM inventory_item WHERE removed_at IS NULL AND quantity > 0 "
     "AND expiration_date < CURRENT_DATE"),
    ("items with zero quantity",
     "SELECT count(*) FROM inventory_item WHERE removed_at IS NULL AND quantity = 0"),
    ("flagged rows awaiting review",
     "SELECT count(*) FROM inventory_item WHERE removed_at IS NULL AND flagged AND reviewed_at IS NULL"),
    ("product names listed under more than one manufacturer",
     "SELECT count(*) FROM (SELECT lower(name) FROM supply GROUP BY lower(name) "
     "HAVING count(DISTINCT manufacturer_id) > 1) x"),
    ("active rows sharing the same sheet row number",
     "SELECT count(*) FROM (SELECT sheet_row FROM inventory_item WHERE removed_at IS NULL "
     "GROUP BY sheet_row HAVING count(*) > 1) x"),
    ("requests with no line items",
     "SELECT count(*) FROM shipment s WHERE NOT EXISTS "
     "(SELECT 1 FROM shipment_item i WHERE i.shipment_id = s.shipment_id)"),
    ("last sync had an error",
     "SELECT count(*) FROM (SELECT error FROM sync_run ORDER BY run_id DESC LIMIT 1) x "
     "WHERE error IS NOT NULL"),
    ("hours since last successful sync (warn if > 2)",
     "SELECT CASE WHEN h > 2 THEN h ELSE 0 END FROM (SELECT COALESCE(floor(extract(epoch FROM "
     "now() - max(finished_at)) / 3600), 9999)::INT AS h FROM sync_run WHERE error IS NULL) x"),
]


def run_checks(conn) -> list[tuple[str, int]]:
    """Returns [(label, count)] for every check (0 = fine)."""
    out = []
    with conn.cursor() as cur:
        for label, sql in CHECKS:
            cur.execute(sql)
            out.append((label, int(cur.fetchone()[0] or 0)))
    return out
