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
import os
import re
from collections import defaultdict

import psycopg
from psycopg.types.json import Jsonb

from b4p import normalize as N
from b4p import sources

log = logging.getLogger("b4p.sync")

# If a sync would mark more than this share of the active items removed,
# something is probably wrong with the feed (truncated, filtered, wrong tab), so
# nothing is removed and a warning is recorded instead. Override for one run
# with B4P_MAX_REMOVE_FRACTION=1 when a big deletion is real. Tiny databases
# may always remove up to MIN_REMOVE_ALLOWED rows.
DEFAULT_MAX_REMOVE_FRACTION = 0.15   # vs the most active items in the last 24h
MIN_REMOVE_ALLOWED = 5
# If fewer than half of the sheet's rows are items we already know, the sync is
# probably reading the wrong tab/sheet: it changes nothing (B4P_MIN_KEY_OVERLAP=0
# to accept a genuinely new sheet). Both guards only apply once the database has
# MIN_ACTIVE_FOR_GUARDS active items, so a first load works.
DEFAULT_MIN_KEY_OVERLAP = 0.5
MIN_ACTIVE_FOR_GUARDS = 20


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
        self.reset()

    def reset(self):
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
# A renamed/missing column would silently rewrite every row (all quantities 0,
# every item re-created, ...), so the sync refuses to run without these.
REQUIRED_HEADERS = ["Name", "Quantity", "Image URL", "Manufacturer Name", "Date of Expiration",
                    "Pallet Number", "Box Number"]
CATEGORY_HEADERS = ["Subcategory", "Subcategories", "Category"]


class SyncAbort(RuntimeError):
    pass


def check_headers(rows: list[tuple[int, dict]]) -> None:
    if not rows:
        raise SyncAbort("inventory feed has no rows")
    headers = set()
    for _, r in rows:
        headers.update(r)
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if not any(h in headers for h in CATEGORY_HEADERS):
        missing.append(" or ".join(CATEGORY_HEADERS))
    if missing:
        raise SyncAbort(f"sheet is missing required column(s) {missing} (renamed?); nothing was changed")


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


_ALT_KEY = re.compile(r"^(.*)#([0-9a-f]{12})(?:#\d+)?$")


def base_of(key: str) -> str:
    """'https://...#0123456789ab#2' -> 'https://...' (copy keys -> their URL)."""
    m = _ALT_KEY.match(key)
    return m[1] if m else key


def assign_keys(rows: list[tuple[int, dict]], existing: dict[str, tuple[str, int, bool]],
                warnings: list[str]) -> list[tuple[str, int, dict]]:
    """Give every non-blank row its stable identity (source_key).

    existing: {source_key: (content_hash, sheet_row, active)} from the database.

    Normally the key is the Image URL. When several rows share one URL (a
    copy-pasted row) the copies are told apart like this:
      1. a row whose cells are unchanged keeps the key it had (exact match);
      2. remaining rows are paired, in sheet order, with the remaining active
         keys for that URL (so editing a copy keeps its id and quantity);
      3. anything left over is new: URL#<hash of its cells>.
    Deleting one copy therefore never moves its id/quantity onto another.
    """
    groups: dict[str, list[tuple[int, dict, str]]] = defaultdict(list)
    for sheet_row, row in rows:
        groups[N.source_key(row)].append((sheet_row, row, N.content_hash(row)))
    by_base: dict[str, list[str]] = defaultdict(list)
    for k in existing:
        by_base[base_of(k)].append(k)

    out, used = [], set()
    for base, members in groups.items():
        if len(members) > 1:
            warnings.append(f"{len(members)} rows share the same identity {base[:80]} "
                            f"(sheet rows {', '.join(str(m[0]) for m in members[:5])})")
        known = by_base.get(base, [])
        keys: list[str | None] = [None] * len(members)
        # 1. exact content matches
        by_content = {}
        for k in sorted(known, key=lambda k: (not existing[k][2], k != base)):
            by_content.setdefault(existing[k][0], []).append(k)
        for i, (_, _, ch) in enumerate(members):
            for k in by_content.get(ch, []):
                if k not in used:
                    keys[i] = k
                    used.add(k)
                    break
        # 2. edited rows: pair with the remaining active keys, in sheet-row order
        free = sorted((k for k in known if k not in used and existing[k][2]),
                      key=lambda k: (existing[k][1] or 0, k))
        todo = [i for i in range(len(members)) if keys[i] is None]
        for i, k in zip(sorted(todo, key=lambda i: members[i][0]), free):
            keys[i] = k
            used.add(k)
        # 3. new rows
        for i, (_, _, ch) in enumerate(members):
            if keys[i] is None:
                key = base if (len(members) == 1 and base not in existing) else f"{base}#{ch[:12]}"
                n = 1
                while key in used or (key in existing and key != base):
                    n += 1
                    key = f"{base}#{ch[:12]}#{n}"
                keys[i] = key
                used.add(key)
        out += [(keys[i], m[0], m[1]) for i, m in enumerate(members)]
    return out


def sync_inventory(cur, rows: list[tuple[int, dict]], warnings: list[str]) -> dict:
    """rows = [(sheet_row, {header: cell})]. Returns counts."""
    stats = {"rows_seen": len(rows), "items_added": 0, "items_updated": 0,
             "items_removed": 0, "rows_skipped": 0}
    check_headers(rows)
    rules = load_rules(cur)
    ids = _Ids(cur)

    cur.execute("SELECT source_key, item_id, source_hash, raw, quantity_set_at, "
                "removed_at IS NOT NULL, sheet_row FROM inventory_item")
    existing, info = {}, {}  # key -> (item_id, source_hash, raw, quantity_set_at, removed)
    for r in cur.fetchall():
        existing[r[0]] = r[1:6]
        info[r[0]] = (N.content_hash(r[3]), r[6], not r[5])

    nonblank = []
    for sheet_row, row in rows:
        row = N.sanitize_row(row)
        if any(N.clean(v) for v in row.values()):
            nonblank.append((sheet_row, row))
        else:
            stats["rows_skipped"] += 1  # blank row
    keyed = assign_keys(nonblank, info, warnings)
    seen = {k for k, _, _ in keyed}
    active = [k for k, v in existing.items() if not v[4]]

    # Wrong tab / different sheet? If most of the feed is unknown to us, stop.
    min_overlap = env_float("B4P_MIN_KEY_OVERLAP", DEFAULT_MIN_KEY_OVERLAP)
    if len(active) >= MIN_ACTIVE_FOR_GUARDS and keyed:
        matched = len(seen & set(active))
        if matched < min_overlap * len(keyed):
            warnings.append(f"only {matched} of {len(keyed)} sheet rows match items already in the database "
                            f"(need {min_overlap:.0%}); wrong tab or a different sheet? NOTHING was changed. "
                            f"If this sheet really replaced the old one, re-run with B4P_MIN_KEY_OVERLAP=0.")
            stats["refused"] = "wrong tab or sheet: too few known rows"
            return stats

    for key, sheet_row, row in keyed:
        h = N.row_hash(row, sheet_row)
        old = existing.get(key)
        if old and old[1] == h and not old[4]:
            continue  # unchanged
        try:
            with cur.connection.transaction():  # savepoint: a bad row can't sink the run
                added = _write_row(cur, ids, rules, key, sheet_row, row, h, old, warnings)
        except Exception as e:  # noqa: BLE001 - report and carry on
            ids.reset()  # cached ids may have been rolled back with the savepoint
            warnings.append(f"sheet row {sheet_row}: skipped ({type(e).__name__}: {str(e).strip()[:150]})")
            stats["rows_skipped"] += 1
            continue
        stats["items_added" if added else "items_updated"] += 1

    gone = [k for k in active if k not in seen]
    # Compare against the most items we had in the last 24 hours, so a feed that
    # shrinks a little every run can't remove a lot in total.
    # (= items active at any moment in the last 24h: still active, or removed recently)
    cur.execute("SELECT count(*) FROM inventory_item WHERE removed_at IS NULL "
                "OR removed_at > now() - interval '24 hours'")
    baseline = max(cur.fetchone()[0], len(active))
    frac = env_float("B4P_MAX_REMOVE_FRACTION", DEFAULT_MAX_REMOVE_FRACTION)
    lost = baseline - (len(active) - len(gone))
    if gone and lost > max(frac * baseline, MIN_REMOVE_ALLOWED):
        warnings.append(f"{len(gone)} of {len(active)} active items are missing from the sheet ({lost} fewer than "
                        f"the {baseline} we had in the last 24h); that is more than B4P_MAX_REMOVE_FRACTION="
                        f"{frac:g} allows, so NOTHING was marked removed. If the deletion is real, re-run "
                        f"with a higher B4P_MAX_REMOVE_FRACTION.")
        stats["refused"] = "too many items missing from the sheet; removals skipped"
    elif gone:
        cur.execute("UPDATE inventory_item SET removed_at = now(), updated_at = now() "
                    "WHERE source_key = ANY(%s) AND removed_at IS NULL", (gone,))
        stats["items_removed"] = cur.rowcount
    return stats


def _write_row(cur, ids, rules, key, sheet_row, row, h, old, warnings) -> bool:
    """Insert or update one sheet row. Returns True if inserted."""
    f = N.read_inventory_row(row, rules)
    for p in f["problems"]:
        warnings.append(f"sheet row {sheet_row}: {p}")
    mfr_id = ids.manufacturer(f["manufacturer"])
    values = {
        "sheet_row": sheet_row,
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
        "review_reason": f["review_reason"],
        "raw": Jsonb(row),
        "source_hash": h,
    }
    if f["warehouse_date"]:
        values["warehouse_date"] = f["warehouse_date"]
    if old is None:
        values.update(source_key=key, quantity=f["quantity"])
        cur.execute(f"INSERT INTO inventory_item ({', '.join(values)}) "
                    f"VALUES ({', '.join(['%s'] * len(values))})", list(values.values()))
        return True

    item_id, _, old_raw, old_set_at, _ = old
    values["removed_at"] = None
    sets = [f"{c} = %s" for c in values]
    params = list(values.values())
    # A quantity set from a dashboard (set_item_quantity) is kept until the
    # sheet's Quantity cell itself changes. The CASE re-checks quantity_set_at
    # at write time, so a dashboard edit made WHILE this sync runs also wins.
    if old_set_at is None or N.clean(old_raw.get("Quantity")) != N.clean(row.get("Quantity")):
        sets += ["quantity = CASE WHEN quantity_set_at IS NOT DISTINCT FROM %s THEN %s ELSE quantity END",
                 "quantity_set_at = CASE WHEN quantity_set_at IS NOT DISTINCT FROM %s THEN NULL "
                 "ELSE quantity_set_at END"]
        params += [old_set_at, f["quantity"], old_set_at]
    cur.execute(f"UPDATE inventory_item SET {', '.join(sets)}, updated_at = now() WHERE item_id = %s",
                [*params, item_id])
    return False


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
        if not isinstance(rec, dict):
            warnings.append(f"request feed entry that is not an object skipped: {str(rec)[:60]}")
            continue
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
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{rid}: not imported ({type(e).__name__}: {str(e).strip()[:120]})")
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
            source = "unavailable"
            errors.append(f"inventory feed: {e}")
    req_source = "given"
    if requests is None:
        try:
            req_source, requests = sources.fetch_requests()
        except (sources.FeedError, ValueError) as e:
            req_source = "unavailable"
            errors.append(f"requests feed: {e}")

    if inventory is not None:
        try:
            check_headers(inventory)
        except SyncAbort as e:
            errors.append(f"inventory: {e}")
            inventory = None

    cols = ["source", "rows_seen", "items_added", "items_updated", "items_removed",
            "rows_skipped", "requests_added", "lines_added", "active_items"]
    stats = {"source": f"{source or 'given'}; requests: {req_source}", **{c: 0 for c in cols[1:]}}

    def record(cur):
        cur.execute("SELECT count(*) FROM inventory_item WHERE removed_at IS NULL")
        stats["active_items"] = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO sync_run (finished_at, {', '.join(cols)}, warnings, error) "
            f"VALUES (now(), {', '.join(['%s'] * len(cols))}, %s, %s) RETURNING run_id",
            [stats.get(c, 0) if c != "source" else stats["source"] for c in cols]
            + [Jsonb(warnings), "; ".join(errors) or None])
        stats["run_id"] = cur.fetchone()[0]
        if stats.get("refused"):
            cur.execute("UPDATE sync_run SET refused = %s WHERE run_id = %s", (stats["refused"], stats["run_id"]))

    try:
        with conn.transaction(), conn.cursor() as cur:
            # one sync at a time (cron + a manual run could overlap)
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('b4p.sync'))")
            if inventory is not None:
                stats.update(sync_inventory(cur, inventory, warnings))
            if requests is not None:
                stats.update(sync_requests(cur, requests, warnings))
            record(cur)
    except Exception as e:  # noqa: BLE001 - the whole run was rolled back; say so
        for k in cols[1:]:
            stats[k] = 0
        errors.append(f"sync failed and was rolled back: {type(e).__name__}: {str(e).strip()[:300]}")
        with conn.transaction(), conn.cursor() as cur:
            record(cur)
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

