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
    ("last sync had an error or was refused by a safety guard",
     "SELECT count(*) FROM (SELECT error, refused FROM sync_run ORDER BY run_id DESC LIMIT 1) x "
     "WHERE error IS NOT NULL OR refused IS NOT NULL"),
    ("hours since last successful sync (warn if > 2)",
     "SELECT CASE WHEN h > 2 THEN h ELSE 0 END FROM (SELECT COALESCE(floor(extract(epoch FROM "
     "now() - max(finished_at)) / 3600), 9999)::INT AS h FROM sync_run WHERE error IS NULL AND COALESCE(source, '') NOT LIKE 'unavailable%') x"),
]


# Where to look: label -> SQL returning (text) lines, worst first. Names go through
# redact_phi so a patient identifier never reaches a terminal or log.
_LOC = ("'row ' || COALESCE(i.sheet_row::TEXT, '?') || COALESCE(' (' || b.box_type || ' box ' || "
        "COALESCE(b.box_number::TEXT, '?') || ', pallet ' || COALESCE(b.pallet_number::TEXT, '?') || ')', '')")
_FROM = "FROM inventory_item i LEFT JOIN box b ON b.box_id = i.box_id WHERE i.removed_at IS NULL"
DETAILS = {
    "expired stock still on the shelf":
        f"SELECT {_LOC} || ': ' || left(redact_phi(i.raw_name), 60) || ' x' || i.quantity || ', expired ' || "
        f"i.expiration_date {_FROM} AND i.quantity > 0 AND i.expiration_date < CURRENT_DATE "
        "ORDER BY i.expiration_date, i.sheet_row",
    "items with zero quantity":
        f"SELECT {_LOC} || ': ' || left(redact_phi(i.raw_name), 60) {_FROM} AND i.quantity = 0 "
        "ORDER BY b.pallet_number, b.box_number, i.sheet_row",
    "flagged rows awaiting review":
        f"SELECT {_LOC} || ': ' || left(redact_phi(i.raw_name), 50) || ' -- ' || "
        f"COALESCE(i.review_reason, 'flagged') {_FROM} AND i.flagged AND i.reviewed_at IS NULL "
        "ORDER BY i.sheet_row",
    "product names listed under more than one manufacturer":
        "SELECT left(redact_phi(min(s.name)), 50) || ' -- ' || string_agg(DISTINCT COALESCE(m.name::TEXT, '(none)'), ' / ') "
        "FROM supply s LEFT JOIN manufacturer m ON m.manufacturer_id = s.manufacturer_id "
        "GROUP BY lower(s.name) HAVING count(DISTINCT s.manufacturer_id) > 1 ORDER BY lower(s.name)",
    "active rows sharing the same sheet row number":
        "SELECT 'row ' || sheet_row || ' x' || count(*) FROM inventory_item WHERE removed_at IS NULL "
        "GROUP BY sheet_row HAVING count(*) > 1 ORDER BY sheet_row",
    "requests with no line items":
        "SELECT s.request_id FROM shipment s WHERE NOT EXISTS "
        "(SELECT 1 FROM shipment_item i WHERE i.shipment_id = s.shipment_id) ORDER BY s.request_id",
    "last sync had an error or was refused by a safety guard":
        "SELECT 'run #' || run_id || ': ' || left(COALESCE(error, 'refused: ' || refused), 200) FROM sync_run ORDER BY run_id DESC LIMIT 1",
}


def details(conn, label: str, limit: int = 10) -> list[str]:
    """Up to `limit` example lines for a check (empty if none / not available)."""
    sql = DETAILS.get(label)
    if not sql:
        return []
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) d LIMIT %s", (limit,))
        return [r[0] for r in cur.fetchall()]


def run_checks(conn) -> list[tuple[str, int]]:
    """Returns [(label, count)] for every check (0 = fine)."""
    out = []
    with conn.cursor() as cur:
        for label, sql in CHECKS:
            cur.execute(sql)
            out.append((label, int(cur.fetchone()[0] or 0)))
    return out
