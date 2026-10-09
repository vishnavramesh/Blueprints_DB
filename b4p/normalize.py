"""Turn messy sheet cells into clean values.

Every function here is pure (no database, no network) so it is easy to test:
see tests/test_normalize.py for the real-world inputs each rule handles.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Lookup rules. The database has editable copies of these (category_alias,
# manufacturer_alias, box_type_alias); the sync merges them in at startup.
# ---------------------------------------------------------------------------
APPROVED_CATEGORIES = [
    "IV Supplies", "Misc Surgical", "Wound Care & Bandages", "Needles & Syringes",
    "Catheters & Briefs", "Airway & Oxygen", "Vitals & Home Health", "Gloves",
    "Apparel (Gowns, Shoe Covers, etc.)", "Masks & Face Shields", "Sutures",
    "Misc Non-Medical",
]
FALLBACK_CATEGORY = "Misc Non-Medical"
CATEGORY_ALIASES = {  # lower-case alias -> approved name
    "unspecified": FALLBACK_CATEGORY,
    "uncategorized": FALLBACK_CATEGORY,
    "medical supplies": FALLBACK_CATEGORY,
}
MANUFACTURER_ALIASES = {  # lower-case alias -> canonical name
    "bd": "BD", "becton, dickinson and company": "BD", "becton dickinson": "BD",
    "cardinalhealth": "Cardinal Health", "cardinal health 200, llc": "Cardinal Health",
    "medline industries": "Medline", "medline industries, lp": "Medline", "medline": "Medline",
    "owens & minor, inc.": "Owens & Minor",
}
NO_MANUFACTURER = {"", "unspecified", "unknown", "n/a", "na", "none"}
BOX_TYPE_ALIASES = {"STN": "ST/N", "TCRC": "TC/RC", "TC/RC/": "TC/RC", "TCRC/": "TC/RC"}
KNOWN_BOX_TYPES = {"LEGACY", "TC/RC", "ST/N", "DME", "ER", "MISC", "PH/PPE",
                   "IV-PROC", "PT-CARE", "WOUND"}

# Unicode dashes people (and Google Sheets) put in "Non-Medical"
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-",
                         "—": "-", "−": "-"})


def clean(value) -> str:
    """Cell -> stripped string ('' for None)."""
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------------
# Category / manufacturer
# ---------------------------------------------------------------------------
def norm_category(raw, aliases: dict[str, str] | None = None) -> str:
    """Approved category name. Unknown or blank -> 'Misc Non-Medical'
    (same rule as the internal dashboard)."""
    key = clean(raw).replace("â\u0080\u0090", "-").translate(_DASHES).lower()
    key = re.sub(r"\s+", " ", key)
    for name in APPROVED_CATEGORIES:
        if key == name.lower():
            return name
    return (aliases or CATEGORY_ALIASES).get(key, FALLBACK_CATEGORY)


def norm_manufacturer(raw, aliases: dict[str, str] | None = None) -> str | None:
    """Canonical manufacturer name, or None if blank/unknown."""
    name = re.sub(r"\s+", " ", clean(raw))
    if name.lower() in NO_MANUFACTURER:
        return None
    return (aliases or MANUFACTURER_ALIASES).get(name.lower(), name)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def first_int(raw) -> int | None:
    """First run of digits in the cell ('Pallet 7' -> 7, '1,200' -> 1200)."""
    m = re.search(r"\d+", clean(raw).replace(",", ""))
    return int(m.group()) if m else None


def parse_qty(raw) -> int:
    """'20 (individual pens NOT boxes)' -> 20, '-' / 'N/A' -> 0, huge -> 999999."""
    n = first_int(raw)
    return 0 if n is None else min(n, 999_999)


def parse_pallet(raw) -> int | None:
    n = first_int(raw)
    return n if n is not None and n <= 2_147_483_647 else None


def parse_money(raw) -> float | None:
    """'$1,234.50' -> 1234.5; blank/garbage -> None."""
    m = re.search(r"\d+(?:\.\d+)?", clean(raw).replace(",", ""))
    return float(m.group()) if m else None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m/%Y", "%Y")


def parse_date(raw) -> date | None:
    """Expiration date. Accepts 2027-03-15, 3/15/2027, 3/15/27, 11/2026 (-> 1st
    of month), 2028 (-> Jan 1). Anything else -> None."""
    s = clean(raw)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_timestamp(raw) -> datetime | None:
    """ISO timestamp from the requests feed ('2026-05-23T05:23:06.63+00:00')."""
    s = clean(raw)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------
def norm_box_type(raw, aliases: dict[str, str] | None = None) -> str | None:
    """Box label from the sheet's box-category column ('STN' -> 'ST/N')."""
    s = re.sub(r"\s+", " ", clean(raw))
    if not s:
        return None
    table = {k.upper(): v for k, v in (aliases or BOX_TYPE_ALIASES).items()}
    return table.get(s.upper(), s)


def parse_box_label(raw, aliases: dict[str, str] | None = None) -> tuple[str, int] | None:
    """Old-style combined label in the "Box Number" cell: 'ER #1' / 'MISC 2' /
    'TC/RC 11' -> (type, number); bare '11' -> ('LEGACY', 11). Ambiguous text
    such as 'ER - 1' or 'DME box 1' -> None (kept as-is in the raw row)."""
    s = clean(raw).upper()
    if re.fullmatch(r"\d+", s):
        n = int(s)
        return ("LEGACY", n) if 0 < n <= 2_147_483_647 else None
    m = re.match(r"^([A-Z][A-Z/\-]*?)\s*#?\s*(\d+)$", s)
    if not m:
        return None
    table = {k.upper(): v for k, v in (aliases or BOX_TYPE_ALIASES).items()}
    btype = table.get(m.group(1), m.group(1))
    n = int(m.group(2))
    if btype not in KNOWN_BOX_TYPES or not 0 < n <= 2_147_483_647:
        return None
    return btype, n


# ---------------------------------------------------------------------------
# Request status
# ---------------------------------------------------------------------------
STATUSES = ["Under Review", "Approved", "Shipped", "Delivered", "Cancelled", "Denied"]
_STATUS_ALIASES = {"under_review": "Under Review", "pending": "Under Review",
                   "requested": "Under Review", "canceled": "Cancelled"}


def norm_status(raw) -> str | None:
    """Display status ('approved' -> 'Approved'); None if unrecognised.
    Mirrors b4p_status() in sql/schema.sql."""
    key = clean(raw).lower()
    for s in STATUSES:
        if key == s.lower():
            return s
    return _STATUS_ALIASES.get(key)


def parse_bool(raw) -> bool:
    return clean(raw).lower() in ("true", "yes", "y", "1")


# ---------------------------------------------------------------------------
# A whole inventory row
# ---------------------------------------------------------------------------
def _first(row: dict, *headers: str) -> str:
    for h in headers:
        v = clean(row.get(h))
        if v:
            return v
    return ""


def needs_review(name: str, review: str) -> bool:
    """Unreadable scans ('{...}', blank, 'Unknown') and rows marked for review."""
    bad_name = not name or name.startswith("{") or name.lower() in ("unknown", "n/a")
    return bad_name or review.lower() not in ("", "no", "no review needed", "false")


def read_inventory_row(row: dict, rules: dict | None = None, today: date | None = None) -> dict:
    """One sheet row (header -> cell text) -> normalized fields for inventory_item.

    Handles both sheet layouts we have seen:
      * current:  Category = box label ('ST/N'), Subcategory = approved category,
                  Box Number = '3'
      * older:    Category = approved category, Box Number = 'ER #1'
    A blank Subcategory means 'Misc Non-Medical' (the box label in Category is
    NOT used as a category).
    """
    rules = rules or {}
    has_sub = any(h in row for h in ("Subcategory", "Subcategories"))
    category_cell = _first(row, "Subcategories", "Subcategory") if has_sub else clean(row.get("Category"))
    # box label: 'Box Category', or 'Main' ([AVAILABLE] tab), or 'Category' when
    # the approved category lives in a Subcategory column
    box_label_cell = _first(row, "Box Category", "Main") or (clean(row.get("Category")) if has_sub else "")
    box_cell = clean(row.get("Box Number"))

    if box_label_cell:  # dedicated box-label column + plain number
        box_type = norm_box_type(box_label_cell, rules.get("box_type"))
        box_number = first_int(box_cell)
    else:  # old combined label in the Box Number cell
        parsed = parse_box_label(box_cell, rules.get("box_type"))
        box_type, box_number = parsed if parsed else (None, first_int(box_cell))

    name = clean(row.get("Name"))
    review = clean(row.get("Review"))
    lot = clean(row.get("Lot Number"))
    days = first_int(row.get("Days in Warehouse"))
    today = today or date.today()
    return {
        "raw_name": name,
        "general_name": clean(row.get("General")) or None,
        "category": norm_category(category_cell, rules.get("category")),
        "manufacturer": norm_manufacturer(row.get("Manufacturer Name"), rules.get("manufacturer")),
        "quantity": parse_qty(row.get("Quantity")),
        "unit_value": parse_money(_first(row, "Price per unit", "Unit Value")),
        "lot_number": lot if lot.lower() not in ("", "unspecified", "n/a", "na") else None,
        "expiration_date": parse_date(row.get("Date of Expiration")),
        "warehouse_date": (today - timedelta(days=days)) if days is not None else None,
        "box_type": box_type,
        "box_number": box_number,
        "pallet_number": parse_pallet(row.get("Pallet Number")),
        "image_url": clean(row.get("Image URL")) or None,
        "notes": clean(row.get("Notes")) or None,
        "review": review or None,
        "flagged": needs_review(name, review),
        "sheet_supply_id": _first(row, "Supply ID", "Supply Id", "SupplyId", "Item ID") or None,
    }


def source_key(row: dict) -> str:
    """Stable identity of a sheet row: its Image URL (each scan has its own
    photo). Rows without one fall back to a hash of what's on the label."""
    url = clean(row.get("Image URL"))
    if url:
        return url
    parts = [clean(row.get(h)) for h in ("Name", "Manufacturer Name", "Lot Number",
                                         "Date of Expiration", "General", "Notes")]
    return "hash:" + hashlib.md5("\x1f".join(parts).encode()).hexdigest()


def row_hash(row: dict, sheet_row: int | None) -> str:
    """Changes whenever any cell (or the row's position) changes."""
    payload = json.dumps({"row": row, "sheet_row": sheet_row}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode()).hexdigest()
