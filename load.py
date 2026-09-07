#!/usr/bin/env python3
"""Load B4P dashboard exports (supplies.json, requests.json) into Postgres."""
import hashlib, json, re, sys
from datetime import datetime
from pathlib import Path
import psycopg

DATA = Path(__file__).parent / "data"

CATEGORY_IDS = {
    "iv supplies": 1, "misc surgical": 2, "wound care & bandages": 3,
    "needles & syringes": 4, "catheters & briefs": 5, "airway & oxygen": 6,
    "vitals & home health": 7, "gloves": 8,
    "apparel (gowns, shoe covers, etc.)": 9, "masks & face shields": 10,
    "sutures": 11, "misc non-medical": 12, "medical supplies": 13,
}
MFR_ALIASES = {  # variant -> canonical
    "bd": "BD", "becton, dickinson and company": "BD", "becton dickinson": "BD",
    "cardinalhealth": "Cardinal Health", "cardinal health 200, llc": "Cardinal Health",
    "medline industries": "Medline", "medline industries, lp": "Medline", "medline": "Medline",
    "owens & minor, inc.": "Owens & Minor",
}
BOX_TYPE_FIX = {"STN": "ST/N", "TCRC": "TC/RC", "TC/RC/": "TC/RC", "TCRC/": "TC/RC"}
VALID_BOX_TYPES = {"LEGACY", "TC/RC", "ST/N", "DME", "ER", "MISC", "PH/PPE", "IV-PROC", "PT-CARE", "WOUND"}
STATUS_MAP = {"under review": "under_review", "approved": "approved", "shipped": "shipped",
              "delivered": "delivered", "cancelled": "cancelled", "denied": "cancelled"}

def norm_category(raw):
    key = (raw or "").strip().lower().replace("‐", "-").replace("‑", "-")
    return CATEGORY_IDS.get(key, 99)

def norm_mfr(raw):
    name = (raw or "").strip()
    if not name or name.lower() in ("unspecified", "unknown", "n/a"):
        return None
    return MFR_ALIASES.get(name.lower(), name)

def parse_qty(raw):
    m = re.search(r"\d+", str(raw or ""))
    return min(int(m.group()), 999_999) if m else 0

def parse_date(raw):
    s = str(raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m/%Y", "%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

def parse_box(raw):
    """'ER #1' / 'MISC 2' / 'TC/RC 11' -> (type, number); bare '11' -> LEGACY-11"""
    s = str(raw or "").strip().upper()
    if re.fullmatch(r"\d+", s):
        return ("LEGACY", int(s))
    m = re.match(r"^([A-Z][A-Z/\-]*?)\s*#?\s*(\d+)$", s)
    if not m:
        return None
    btype = BOX_TYPE_FIX.get(m.group(1), m.group(1))
    return (btype, int(m.group(2))) if btype in VALID_BOX_TYPES else None

def parse_pallet(raw):
    m = re.search(r"\d+", str(raw or ""))
    return int(m.group()) if m else None


def main():
    supplies = json.loads((DATA / "supplies.json").read_text())
    requests_ = json.loads((DATA / "requests.json").read_text())
    stats = {"items": 0, "flagged": 0, "no_box": 0, "supplies": 0}

    with psycopg.connect(dbname="b4p", autocommit=False) as conn, conn.cursor() as cur:
        mfr_cache, box_cache, supply_cache = {}, {}, {}

        def mfr_id(name):
            if name is None:
                return None
            if name not in mfr_cache:
                cur.execute(
                    "INSERT INTO manufacturer (canonical_name) VALUES (%s) "
                    "ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name "
                    "RETURNING manufacturer_id", (name,))
                mfr_cache[name] = cur.fetchone()[0]
            return mfr_cache[name]

        def box_id(btype, bnum, pallet):
            key = (btype, bnum)
            if key not in box_cache:
                cur.execute(
                    "INSERT INTO box (box_type, box_number, pallet_number) VALUES (%s,%s,%s) "
                    "ON CONFLICT (box_type, box_number) DO UPDATE SET pallet_number = COALESCE(box.pallet_number, EXCLUDED.pallet_number) "
                    "RETURNING box_id", (btype, bnum, pallet))
                box_cache[key] = cur.fetchone()[0]
            return box_cache[key]

        # ---------------- supplies -> supply + inventory_item ----------------
        for row in supplies:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            mid = mfr_id(norm_mfr(row.get("Manufacturer Name")))
            cat = norm_category(row.get("Category"))
            flagged = name.startswith("{") or (
                (row.get("Review") or "").strip() not in ("", "No review needed"))

            skey = (mid, name.lower())
            if skey not in supply_cache:
                cur.execute(
                    "INSERT INTO supply (manufacturer_id, category_id, general_name, name) "
                    "VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (manufacturer_id, name) DO UPDATE SET general_name = COALESCE(supply.general_name, EXCLUDED.general_name) "
                    "RETURNING supply_id",
                    (mid, cat, (row.get("General") or "").strip() or None, name))
                supply_cache[skey] = cur.fetchone()[0]
                stats["supplies"] += 1
            sid = supply_cache[skey]

            box = parse_box(row.get("Box Number"))
            bid = box_id(*box, parse_pallet(row.get("Pallet Number"))) if box else None
            if not box and str(row.get("Box Number") or "").strip():
                stats["no_box"] += 1

            lot = (row.get("Lot Number") or "").strip()
            src_hash = hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()
            cur.execute(
                "INSERT INTO inventory_item (supply_id, box_id, quantity, lot_number, "
                " expiration_date, flagged, raw_name, image_url, notes, source_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (source_hash) DO NOTHING",
                (sid, bid, parse_qty(row.get("Quantity")),
                 lot if lot.lower() not in ("", "unspecified") else None,
                 parse_date(row.get("Date of Expiration")),
                 flagged, name if flagged else None,
                 (row.get("Image URL") or "").strip() or None,
                 (row.get("Notes") or "").strip() or None, src_hash))
            stats["items"] += 1
            stats["flagged"] += flagged

        # ---------------- requests -> requester + shipment + items ----------
        shipments = {}
        for row in requests_:
            org = (row.get("Org Name") or "").strip()
            rid = (row.get("Request ID") or "").strip()
            if not org or not rid:
                continue
            cur.execute(
                "INSERT INTO requester (org_name, org_email) VALUES (%s,%s) "
                "ON CONFLICT (org_name) DO UPDATE SET org_email = COALESCE(requester.org_email, EXCLUDED.org_email) "
                "RETURNING requester_id", (org, (row.get("Org Email") or "").strip() or None))
            req_id = cur.fetchone()[0]
            if rid not in shipments:
                status = STATUS_MAP.get((row.get("Status") or "").strip().lower(), "requested")
                ts = row.get("Timestamp")
                cur.execute(
                    "INSERT INTO shipment (requester_id, request_id, status, requested_at) "
                    "VALUES (%s,%s,%s,COALESCE(%s::timestamptz, now())) "
                    "ON CONFLICT (request_id) DO UPDATE SET status = EXCLUDED.status "
                    "RETURNING shipment_id", (req_id, rid, status, ts))
                shipments[rid] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO shipment_item (shipment_id, item_name, category_id, quantity_requested) "
                "VALUES (%s,%s,%s,%s)",
                (shipments[rid], (row.get("Item Name") or "?").strip(),
                 norm_category(row.get("Category")), parse_qty(row.get("Quantity Requested")) or None))

        conn.commit()

    print(f"supplies: {stats['supplies']} catalog rows, {stats['items']} inventory items "
          f"({stats['flagged']} flagged, {stats['no_box']} box labels unparseable)")
    print(f"requests: {len(shipments)} shipments from {len(requests_)} line items")


if __name__ == "__main__":
    sys.exit(main())
