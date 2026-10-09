"""Copy the live feeds into Postgres. Safe to run as often as you like.

Inventory: each sheet row is matched to inventory_item by source_key (its Image
URL). New rows are inserted, edited rows updated, rows that vanished from the
sheet get removed_at set (never deleted). Unchanged rows are not touched, so a
re-run against the same data adds/updates nothing.

Requests: insert-only. Request ids not yet in the database are imported from
the requests sheet/feed; requests already in the database are never changed
(the database is the source of truth for requests).
"""
from __future__ import annotations

import logging
from collections import defaultdict

import psycopg
from psycopg.types.json import Jsonb

from b4p import normalize as N
from b4p import sources

log = logging.getLogger("b4p.sync")

# If the feed suddenly has far fewer rows than the database, something is
# probably wrong with the feed (wrong tab, half-loaded); don't mark everything
# removed in that case.
MIN_FEED_FRACTION = 0.5


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load_rules(cur) -> dict:
    """Alias tables from the database, merged over the built-in defaults."""
    rules = {"category": dict(N.CATEGORY_ALIASES),
             "manufacturer": dict(N.MANUFACTURER_ALIASES),
             "box_type": dict(N.BOX_TYPE_ALIASES)}
    cur.execute("SELECT lower(a.alias), c.name FROM category_alias a JOIN category c USING (category_id)")
    rules["category"].update(dict(cur.fetchall()))
    cur.execute("SELECT lower(a.alias), m.name FROM manufacturer_alias a JOIN manufacturer m USING (manufacturer_id)")
    rules["manufacturer"].update(dict(cur.fetchall()))
    cur.execute("SELECT upper(alias), code FROM box_type_alias")
    rules["box_type"].update(dict(cur.fetchall()))
    return rules


class _Ids:
    """Get-or-create ids for lookup rows, cached for one sync run."""

    def __init__(self, cur):
        self.cur = cur
        cur.execute("SELECT lower(name), category_id FROM category")
        self.categories = dict(cur.fetchall())
        self.mfr, self.supply, self.box, self.box_types = {}, {}, {}, set()

    def category(self, name: str) -> int:
        return self.categories.get(name.lower()) or self.categories[N.FALLBACK_CATEGORY.lower()]

    def manufacturer(self, name: str | None) -> int | None:
        if name is None:
            return None
        key = name.lower()
        if key not in self.mfr:
            self.cur.execute("INSERT INTO manufacturer (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
            self.cur.execute("SELECT manufacturer_id FROM manufacturer WHERE name = %s::citext", (name,))
            self.mfr[key] = self.cur.fetchone()[0]
        return self.mfr[key]

    def supply_id(self, mfr_id: int | None, name: str) -> int:
        key = (mfr_id, name)
        if key not in self.supply:
            self.cur.execute("INSERT INTO supply (manufacturer_id, name) VALUES (%s, %s) "
                             "ON CONFLICT DO NOTHING", key)
            self.cur.execute("SELECT supply_id FROM supply WHERE manufacturer_id IS NOT DISTINCT FROM %s "
                             "AND name = %s", key)
            self.supply[key] = self.cur.fetchone()[0]
        return self.supply[key]

    def box_id(self, btype: str | None, number: int | None, pallet: int | None) -> int | None:
        if btype is None and number is None and pallet is None:
            return None
        if btype is not None and btype.upper() not in self.box_types:
            self.cur.execute("INSERT INTO box_type (code) VALUES (%s) ON CONFLICT DO NOTHING", (btype,))
            self.cur.execute("SELECT code FROM box_type WHERE code = %s::citext", (btype,))
            btype = self.cur.fetchone()[0]  # use the existing spelling
            self.box_types.add(btype.upper())
        key = (btype.upper() if btype else None, number, pallet)
        if key not in self.box:
            self.cur.execute("INSERT INTO box (box_type, box_number, pallet_number) VALUES (%s, %s, %s) "
                             "ON CONFLICT DO NOTHING", (btype, number, pallet))
            self.cur.execute("SELECT box_id FROM box WHERE box_type IS NOT DISTINCT FROM %s::citext "
                             "AND box_number IS NOT DISTINCT FROM %s AND pallet_number IS NOT DISTINCT FROM %s",
                             (btype, number, pallet))
            self.box[key] = self.cur.fetchone()[0]
        return self.box[key]


def _is_catalog_name(name: str) -> bool:
    return bool(name) and not name.startswith("{") and name.lower() not in ("unknown", "n/a")


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------
def sync_inventory(cur, rows: list[tuple[int, dict]], warnings: list[str]) -> dict:
    """rows = [(sheet_row, {header: cell})]. Returns counts."""
    stats = {"rows_seen": len(rows), "items_added": 0, "items_updated": 0,
             "items_removed": 0, "rows_skipped": 0}
    rules = load_rules(cur)
    ids = _Ids(cur)

    cur.execute("SELECT source_key, item_id, source_hash, raw ->> 'Quantity', "
                "quantity_set_at IS NOT NULL, removed_at IS NOT NULL FROM inventory_item")
    existing = {r[0]: r[1:] for r in cur.fetchall()}

    seen: set[str] = set()
    key_counts: dict[str, int] = defaultdict(int)
    for sheet_row, row in rows:
        if not any(N.clean(v) for v in row.values()):
            stats["rows_skipped"] += 1  # blank row
            continue
        base = N.source_key(row)
        key_counts[base] += 1
        key = base if key_counts[base] == 1 else f"{base}#{key_counts[base]}"
        if key_counts[base] == 2:
            warnings.append(f"duplicate row identity at sheet row {sheet_row} ({base[:80]})")
        seen.add(key)

        h = N.row_hash(row, sheet_row)
        old = existing.get(key)
        if old and old[1] == h and not old[4]:
            continue  # unchanged

        f = N.read_inventory_row(row, rules)
        mfr_id = ids.manufacturer(f["manufacturer"])
        values = {
            "sheet_row": sheet_row,
            "sheet_supply_id": f["sheet_supply_id"],
            "supply_id": ids.supply_id(mfr_id, f["raw_name"]) if _is_catalog_name(f["raw_name"]) else None,
            "raw_name": f["raw_name"],
            "general_name": f["general_name"],
            "category_id": ids.category(f["category"]),
            "manufacturer_id": mfr_id,
            "box_id": ids.box_id(f["box_type"], f["box_number"], f["pallet_number"]),
            "unit_value": f["unit_value"],
            "lot_number": f["lot_number"],
            "expiration_date": f["expiration_date"],
            "image_url": f["image_url"],
            "notes": f["notes"],
            "review": f["review"],
            "flagged": f["flagged"],
            "raw": Jsonb(row),
            "source_hash": h,
        }
        if old is None:
            values.update(source_key=key, quantity=f["quantity"])
            if f["warehouse_date"]:
                values["warehouse_date"] = f["warehouse_date"]
            cols = ", ".join(values)
            cur.execute(f"INSERT INTO inventory_item ({cols}) VALUES ({', '.join(['%s'] * len(values))})",
                        list(values.values()))
            stats["items_added"] += 1
            continue

        item_id, _, old_qty_cell, qty_set, _ = old
        # keep a dashboard-set quantity unless the sheet's Quantity cell changed
        if not qty_set or N.clean(old_qty_cell) != N.clean(row.get("Quantity")):
            values.update(quantity=f["quantity"], quantity_set_at=None)
        if f["warehouse_date"]:
            values["warehouse_date"] = f["warehouse_date"]
        values.update(removed_at=None)
        sets = ", ".join(f"{c} = %s" for c in values)
        cur.execute(f"UPDATE inventory_item SET {sets}, updated_at = now() WHERE item_id = %s",
                    [*values.values(), item_id])
        stats["items_updated"] += 1

    active = [k for k, v in existing.items() if not v[4]]
    gone = [k for k in active if k not in seen]
    if gone and len(seen) < MIN_FEED_FRACTION * len(active):
        warnings.append(f"feed has {len(seen)} rows but database has {len(active)} active items; "
                        f"NOT marking {len(gone)} rows removed (feed looks incomplete)")
    elif gone:
        cur.execute("UPDATE inventory_item SET removed_at = now(), updated_at = now() "
                    "WHERE source_key = ANY(%s) AND removed_at IS NULL", (gone,))
        stats["items_removed"] = cur.rowcount
    return stats


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
def sync_requests(cur, feed: list[dict], warnings: list[str]) -> dict:
    """INSERT-ONLY import of requests from the old requests sheet/feed.

    Request ids already in the database are skipped entirely: once a request is
    here, the database owns it (status changes happen via set_request_status).
    New ids go through create_request() with their original id and timestamp.
    """
    stats = {"requests_added": 0, "lines_added": 0}
    groups: dict[str, list[dict]] = {}
    for rec in feed:
        rid = N.clean(rec.get("Request ID"))
        if not rid or not N.clean(rec.get("Org Name")):
            warnings.append(f"request row without id/org skipped: {rid or '?'}")
            continue
        groups.setdefault(rid, []).append(rec)

    cur.execute("SELECT request_id FROM shipment")
    known = {r[0] for r in cur.fetchall()}

    for rid, recs in groups.items():
        if rid in known:
            continue
        first = recs[0]
        items = []
        for rec in recs:
            qty = N.parse_qty(rec.get("Quantity Requested"))
            if qty < 1:
                warnings.append(f"{rid}: quantity {rec.get('Quantity Requested')!r} imported as 1")
                qty = 1
            items.append({"item_name": N.clean(rec.get("Item Name")) or "?",
                          "category": N.clean(rec.get("Category")),
                          "quantity": qty,
                          "review_flag": N.parse_bool(rec.get("Review Flag"))})
        raw_status = N.clean(first.get("Status"))
        status = N.norm_status(raw_status) or "Under Review"
        if raw_status and N.norm_status(raw_status) is None:
            warnings.append(f"{rid}: unknown status {raw_status!r}, imported as Under Review")
        try:
            with cur.connection.transaction():  # savepoint: one bad request doesn't stop the sync
                cur.execute("SELECT create_request(%s, %s, %s, %s, %s)",
                            (N.clean(first.get("Org Name")), N.clean(first.get("Org Email")),
                             Jsonb(items), rid, N.parse_timestamp(first.get("Timestamp"))))
                cur.execute("UPDATE shipment SET source = 'feed', status = %s WHERE request_id = %s",
                            (status, rid))
        except psycopg.Error as e:
            warnings.append(f"{rid}: not imported ({str(e).strip()[:120]})")
            continue
        stats["requests_added"] += 1
        stats["lines_added"] += len(items)
    return stats


# ---------------------------------------------------------------------------
# one full run
# ---------------------------------------------------------------------------
def run_sync(conn: psycopg.Connection, inventory=None, requests=None, source: str | None = None) -> dict:
    """Fetch (unless data is passed in), sync both feeds in one transaction,
    and record a sync_run row. Returns the counts.

    inventory: [(sheet_row, {header: cell})] or None to fetch live
    requests:  [ {Request ID, Org Name, ...} ] or None to fetch live
    """
    warnings: list[str] = []
    errors: list[str] = []
    if inventory is None:
        try:
            source, inventory = sources.fetch_inventory()
        except sources.FeedError as e:
            errors.append(f"inventory feed: {e}")
    req_source = "given"
    if requests is None:
        try:
            req_source, requests = sources.fetch_requests()
        except (sources.FeedError, ValueError) as e:
            errors.append(f"requests feed: {e}")

    stats = {"source": f"{source or 'given'}; requests: {req_source}"}
    with conn.transaction(), conn.cursor() as cur:
        # one sync at a time (cron + a manual run could overlap)
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('b4p.sync'))")
        if inventory is not None:
            stats.update(sync_inventory(cur, inventory, warnings))
        if requests is not None:
            stats.update(sync_requests(cur, requests, warnings))
        cols = ["source", "rows_seen", "items_added", "items_updated", "items_removed",
                "rows_skipped", "requests_added", "lines_added"]
        cur.execute(
            f"INSERT INTO sync_run (finished_at, {', '.join(cols)}, warnings, error) "
            f"VALUES (now(), {', '.join(['%s'] * len(cols))}, %s, %s) RETURNING run_id",
            [stats.get(c, 0) if c != "source" else stats["source"] for c in cols]
            + [Jsonb(warnings), "; ".join(errors) or None])
        stats["run_id"] = cur.fetchone()[0]
    stats["warnings"] = warnings
    stats["errors"] = errors
    for w in warnings:
        log.warning(w)
    for e in errors:
        log.error(e)
    return stats


def summary(stats: dict) -> str:
    return (f"sync #{stats.get('run_id')} [{stats.get('source')}]: "
            f"{stats.get('rows_seen', 0)} rows, +{stats.get('items_added', 0)} added, "
            f"{stats.get('items_updated', 0)} updated, {stats.get('items_removed', 0)} removed, "
            f"{stats.get('rows_skipped', 0)} skipped | requests +{stats.get('requests_added', 0)}, "
            f"lines +{stats.get('lines_added', 0)}"
            + (f" | {len(stats['warnings'])} warnings" if stats.get("warnings") else "")
            + (f" | ERRORS: {'; '.join(stats['errors'])}" if stats.get("errors") else ""))

