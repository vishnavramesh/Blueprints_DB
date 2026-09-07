#!/usr/bin/env python3
"""B4P inventory monitor: syncs new rows from the AI-app feed into Postgres
and reports errors / changes / inconsistencies. Run it in a terminal:

    python3 monitor.py            # sync + check every 5 min
    python3 monitor.py --once     # single pass
"""
import hashlib, json, subprocess, sys, time
from datetime import datetime
from pathlib import Path
import psycopg

import load  # reuse the normalizers and insert logic

SUPPLIES_URL = "https://b4p-external-view-w26-backend.onrender.com/api/supplies"
REQUESTS_URL = "https://b4p-external-view-w26-backend.onrender.com/requests"
INTERVAL_SEC = 300
LOG = Path(__file__).parent / "monitor.log"

def say(level, msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")

def fetch(url):
    out = subprocess.run(["curl", "-s", "-m", "90", url], capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"curl exit {out.returncode}")
    return json.loads(out.stdout)

CHECKS = [
    ("negative stock (ledger pushed quantity below zero)",
     "SELECT count(*) FROM inventory_item WHERE quantity < 0 HAVING count(*) > 0"),
    ("items reserved for a cancelled/delivered shipment",
     """SELECT i.item_id FROM inventory_item i JOIN shipment s ON s.shipment_id = i.reserved_for
        WHERE s.status IN ('cancelled','delivered') LIMIT 20"""),
    ("expired stock not yet flagged or reserved",
     """SELECT count(*) FROM inventory_item WHERE expiration_date < CURRENT_DATE
        AND NOT flagged AND reserved_for IS NULL HAVING count(*) > 0"""),
    ("supplies with duplicate names under different manufacturers",
     """SELECT lower(name) FROM supply GROUP BY lower(name)
        HAVING count(DISTINCT manufacturer_id) > 1 LIMIT 20"""),
    ("inventory items with zero quantity still in a box",
     """SELECT count(*) FROM inventory_item WHERE quantity = 0 AND box_id IS NOT NULL
        HAVING count(*) > 0"""),
    ("flagged items awaiting review",
     "SELECT count(*) FROM inventory_item WHERE flagged HAVING count(*) > 0"),
]

def counts(cur):
    cur.execute("""SELECT (SELECT count(*) FROM inventory_item),
                          (SELECT count(*) FROM supply),
                          (SELECT count(*) FROM shipment),
                          (SELECT coalesce(sum(quantity),0) FROM inventory_item)""")
    return cur.fetchone()

def sync_supplies(cur, rows):
    """Insert only rows whose source_hash is new. Returns number of new items."""
    cur.execute("SELECT source_hash FROM inventory_item WHERE source_hash IS NOT NULL")
    known = {r[0] for r in cur.fetchall()}
    new = 0
    for row in rows:
        h = hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()
        if h in known:
            continue
        name = (row.get("Name") or "").strip()
        if not name:
            continue
        mid = None
        mfr = load.norm_mfr(row.get("Manufacturer Name"))
        if mfr:
            cur.execute("INSERT INTO manufacturer (canonical_name) VALUES (%s) "
                        "ON CONFLICT (canonical_name) DO UPDATE SET canonical_name=EXCLUDED.canonical_name "
                        "RETURNING manufacturer_id", (mfr,))
            mid = cur.fetchone()[0]
        cur.execute("INSERT INTO supply (manufacturer_id, category_id, general_name, name) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (manufacturer_id, name) DO UPDATE SET name=EXCLUDED.name RETURNING supply_id",
                    (mid, load.norm_category(row.get("Category")),
                     (row.get("General") or "").strip() or None, name))
        sid = cur.fetchone()[0]
        bid = None
        box = load.parse_box(row.get("Box Number"))
        if box:
            cur.execute("INSERT INTO box (box_type, box_number, pallet_number) VALUES (%s,%s,%s) "
                        "ON CONFLICT (box_type, box_number) DO UPDATE SET box_number=EXCLUDED.box_number "
                        "RETURNING box_id", (*box, load.parse_pallet(row.get("Pallet Number"))))
            bid = cur.fetchone()[0]
        flagged = name.startswith("{") or ((row.get("Review") or "").strip() not in ("", "No review needed"))
        lot = (row.get("Lot Number") or "").strip()
        cur.execute("INSERT INTO inventory_item (supply_id, box_id, quantity, lot_number, expiration_date, "
                    "flagged, raw_name, image_url, notes, source_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (source_hash) DO NOTHING",
                    (sid, bid, load.parse_qty(row.get("Quantity")),
                     lot if lot.lower() not in ("", "unspecified") else None,
                     load.parse_date(row.get("Date of Expiration")), flagged,
                     name if flagged else None,
                     (row.get("Image URL") or "").strip() or None,
                     (row.get("Notes") or "").strip() or None, h))
        new += 1
        say("NEW", f"item in: {name[:60]!r} qty={load.parse_qty(row.get('Quantity'))} "
                   f"box={row.get('Box Number') or '-'}{' FLAGGED' if flagged else ''}")
    return new

def sync_requests(cur, rows):
    new = 0
    for row in rows:
        org, rid = (row.get("Org Name") or "").strip(), (row.get("Request ID") or "").strip()
        if not org or not rid:
            continue
        cur.execute("INSERT INTO requester (org_name, org_email) VALUES (%s,%s) "
                    "ON CONFLICT (org_name) DO UPDATE SET org_email=COALESCE(requester.org_email,EXCLUDED.org_email) "
                    "RETURNING requester_id", (org, (row.get("Org Email") or "").strip() or None))
        qid = cur.fetchone()[0]
        status = load.STATUS_MAP.get((row.get("Status") or "").strip().lower(), "requested")
        cur.execute("SELECT shipment_id, status FROM shipment WHERE request_id=%s", (rid,))
        hit = cur.fetchone()
        if hit is None:
            cur.execute("INSERT INTO shipment (requester_id, request_id, status, requested_at) "
                        "VALUES (%s,%s,%s,COALESCE(%s::timestamptz, now())) RETURNING shipment_id",
                        (qid, rid, status, row.get("Timestamp")))
            sid = cur.fetchone()[0]
            cur.execute("INSERT INTO shipment_item (shipment_id, item_name, category_id, quantity_requested) "
                        "VALUES (%s,%s,%s,%s)", (sid, (row.get("Item Name") or "?").strip(),
                        load.norm_category(row.get("Category")),
                        load.parse_qty(row.get("Quantity Requested")) or None))
            say("NEW", f"request {rid} from {org!r}: {row.get('Item Name','?')[:50]}")
            new += 1
        elif hit[1] != status:
            cur.execute("UPDATE shipment SET status=%s WHERE shipment_id=%s", (status, hit[0]))
            say("CHANGE", f"request {rid} ({org}): {hit[1]} -> {status}")
    return new

def run_checks(cur):
    for label, sql in CHECKS:
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            if rows:
                detail = rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else f"{len(rows)} cases"
                say("WARN", f"{label}: {detail}")
        except Exception as e:
            say("ERROR", f"check {label!r} failed: {e}")

def cycle():
    with psycopg.connect(dbname="b4p") as conn, conn.cursor() as cur:
        before = counts(cur)
        try:
            n_items = sync_supplies(cur, fetch(SUPPLIES_URL))
        except Exception as e:
            n_items = 0
            say("ERROR", f"supplies feed unreachable: {e}")
        try:
            n_reqs = sync_requests(cur, fetch(REQUESTS_URL))
        except Exception as e:
            n_reqs = 0
            say("ERROR", f"requests feed unreachable: {e}")
        conn.commit()
        after = counts(cur)
        if n_items or n_reqs:
            say("SYNC", f"+{n_items} items, +{n_reqs} requests "
                        f"(items {before[0]}->{after[0]}, units {before[3]}->{after[3]})")
        else:
            say("OK", f"in sync: {after[0]} items / {after[3]} units / {after[2]} shipments")
        run_checks(cur)

if __name__ == "__main__":
    say("START", f"monitoring {SUPPLIES_URL}")
    while True:
        try:
            cycle()
        except Exception as e:
            say("ERROR", f"cycle failed: {e}")
        if "--once" in sys.argv:
            break
        time.sleep(INTERVAL_SEC)
