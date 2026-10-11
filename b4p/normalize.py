"""Turn messy sheet cells into clean values.

Every function here is pure (no database, no network) so it is easy to test:
see tests/test_normalize.py for the real-world inputs each rule handles.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
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


_SURROGATES = re.compile("[\ud800-\udfff]")


def _safe_text(value) -> str:
    """Text Postgres can store: no NULs, no lone UTF-16 surrogates (half an
    emoji, as the inventory app sometimes writes) -> U+FFFD."""
    return _SURROGATES.sub("\ufffd", str(value).replace("\x00", ""))


def clean(value) -> str:
    """Cell -> stripped, storable string ('' for None)."""
    return "" if value is None else _safe_text(value).strip()


def sanitize_row(row: dict) -> dict:
    """Header -> cell text made storable (cells are kept unstripped)."""
    return {_safe_text(k): ("" if v is None else _safe_text(v)) for k, v in row.items()}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "surrogatepass")).hexdigest()


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
# Numbers. Every parser is bounded so one silly cell can't break the database.
# ---------------------------------------------------------------------------
INT_MAX = 2_147_483_647
MAX_QTY = 999_999
MAX_DAYS_IN_WAREHOUSE = 36_500          # 100 years
MAX_MONEY = 99_999_999.99               # NUMERIC(10,2)


def first_int(raw) -> int | None:
    """First run of digits in the cell ('Pallet 7' -> 7, '1,200' -> 1200)."""
    m = re.search(r"\d+", clean(raw).replace(",", ""))
    return int(m.group()) if m else None


def bounded_int(raw, maximum: int = INT_MAX) -> int | None:
    """first_int, but None when the number is larger than `maximum`."""
    n = first_int(raw)
    return n if n is not None and n <= maximum else None


def is_negative(raw) -> bool:
    return bool(re.match(r"^-\s*\d", clean(raw)))


def parse_qty(raw) -> int:
    """'20 (individual pens NOT boxes)' -> 20, '-' / 'N/A' -> 0, huge -> 999999.
    Negative ('-5') -> 0. Decimals keep the whole part ('0.5' -> 0, '2.5' -> 2)."""
    if is_negative(raw):
        return 0
    n = first_int(raw)
    return 0 if n is None else min(n, MAX_QTY)


def parse_pallet(raw) -> int | None:
    return bounded_int(raw)


def parse_money(raw) -> float | None:
    """'$1,234.50' -> 1234.5, '€5,50' -> 5.5, '1.234,50' -> 1234.5.
    Blank, garbage, negative ('$-4') or > 99,999,999.99 -> None."""
    m = re.search(r"\d[\d.,]*", clean(raw))
    if not m or "-" in clean(raw)[:m.start()]:
        return None
    t = m.group().rstrip(".,")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rindex(",") > t.rindex(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) else t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v <= MAX_MONEY else None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december"]
_MONTHS = {**{m: i for i, m in enumerate(_MONTH_NAMES, start=1)},
           **{m[:3]: i for i, m in enumerate(_MONTH_NAMES, start=1)}, "sept": 9}
MIN_YEAR, MAX_YEAR = 1950, 2200
MAX_YEARS_AHEAD = 10  # an expiration further out than this is probably a typo  # anything outside is a typo, not an expiration date
_EXP_PREFIX = re.compile(r"^(?:exp(?:iry|iration|ires)?(?:\s*date)?|use\s*by|best\s*before)\b\.?\s*:?\s*",
                         re.IGNORECASE)


def _month_end(y: int, m: int) -> date | None:
    if not (1 <= m <= 12 and MIN_YEAR <= y <= MAX_YEAR):
        return None
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return nxt - timedelta(days=1)


def _year(y: str) -> int:
    """Two-digit years are 20xx (these are expiration dates)."""
    return 2000 + int(y) if len(y) == 2 else int(y)


def _day(y: int, m: int, d: int) -> date | None:
    if not MIN_YEAR <= y <= MAX_YEAR:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(raw) -> date | None:
    """Expiration date, or None if blank/unreadable (see date_problem()).
      full date:  2027-03-15, 2027/03/15, 3/15/2027, 3/15/27 (two-digit years are 20xx);
                  15/03/2027 is read as 15 March only when it can't be month/day
      month only -> LAST day of that month: 2027-03, 2027/03, 2027.03, 03/2027,
                  03-2027, Mar 2027, March 2027, Sept 2027, Feb-2027
      year only  -> Dec 31 of that year: 2028
      an 'EXP' / 'Expiry:' / 'Use by' prefix is ignored. Years must be 1950-2200."""
    s = _EXP_PREFIX.sub("", clean(raw)).strip()
    if m := re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s):
        return _day(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})", s):
        y, a, b = _year(m[3]), int(m[1]), int(m[2])
        return _day(y, a, b) or (_day(y, b, a) if a > 12 else None)
    if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s):
        return _month_end(int(m[1]), int(m[2]))
    if m := re.fullmatch(r"(\d{1,2})[/-](\d{4})", s):
        return _month_end(int(m[2]), int(m[1]))
    if m := re.fullmatch(r"([A-Za-z]+)\.?[\s\-/]*(\d{4})", s):
        mon = _MONTHS.get(m[1].lower())
        return _month_end(int(m[2]), mon) if mon else None
    if m := re.fullmatch(r"(\d{4})", s):
        return _month_end(int(m[1]), 12)
    return None


def date_problem(raw) -> str | None:
    """Message if a non-empty expiration cell couldn't be read, else None."""
    s = clean(raw)
    if s and s.lower() not in ("n/a", "na", "none", "-", "unspecified", "no expiration", "unknown") \
            and parse_date(s) is None:
        return f"unreadable Date of Expiration {s[:40]!r} (row flagged for review)"
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


# A patient identifier on a specimen-tube label, e.g. "Pt. ID <number> <code>",
# "Patient XX 1234", "patient ID: 1234 5678". Mirrors b4p_patient_id_pattern()
# in sql/schema.sql (tests/test_privacy.py checks they agree).
_ID_TOKEN = r"(?:(?!ID\b)[A-Z]{1,3}\b|[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*)"
PATIENT_ID_RE = re.compile(
    r"\b(?:[Pp][Tt]\b\.?|[Pp][Aa][Tt][Ii][Ee][Nn][Tt]\b)"
    r"(?:\s*(?:[Ii][Dd]\b|#|[Nn][Oo]\b\.?))?[\s:#,.]*"
    + _ID_TOKEN + r"(?:[\s,]+" + _ID_TOKEN + r"){0,3}")


# Research-subject / specimen ID on trial sample tubes ("A00-000").
# Mirrors b4p_subject_id_pattern() in sql/schema.sql.
SUBJECT_ID_RE = re.compile(r"\b[A-Z][0-9]{2}-[0-9]{3}\b")

# A person's name written after the Patient / Pt keyword: "Patient: Jane Doe",
# "Pt. Name: J. Smith", or without a colon "Patient John Smith" (two capitalised
# words, the first not an ordinary product word such as "Patient Return Electrode").
# Mirrors b4p_patient_name_pattern() in sql/schema.sql.
_NAME_WORD = r"[A-Z][A-Za-z'-]*\.?"
_PRODUCT_WORDS = ("Return|Starter|Slippers?|Use|Monitoring|Monitor|Disposable|Belonging|Belongings|Lift|"
                  "Transfer|Care|Controlled|Warming|Gown|Bed|Safety|Restraint|Positioning|Education|"
                  "Comfort|Handling|Ready|Preoperative|Preparation|Specimen|Sample|Identification|"
                  "Information|Wristband|Wipes?|Isolation|Support|Cart|Chair|Hygiene|Bathing|Skin|"
                  "Lifting|Slide|Sling|Aid|Kit|Supplies|Access|Contact|Cooling|Heating|Side|Ports?|"
                  "Cable|Circuit|Interface|Sitter|Cuff|Lines?|Hose|Tubing|Mask")
PATIENT_NAME_RE = re.compile(
    r"\b(?:[Pp][Tt]\b\.?|[Pp]atient\b)(?:"
    r"(?:\s+[Nn]ame)?\s*:\s*(?!ID\b)" + _NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){0,2}"
    r"|\s+(?!(?:" + _PRODUCT_WORDS + r")\b)[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+)")

# Zero-width / bidi / soft-hyphen characters people (or scanners) slip into text
_INVISIBLE_RE = re.compile("[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def fold(text) -> str:
    """Text as the redaction patterns should see it: no invisible characters,
    full-width / compatibility forms folded (NFKC). Mirrors b4p_fold()."""
    return unicodedata.normalize("NFKC", _INVISIBLE_RE.sub("", clean(text)))


def has_patient_id(text) -> bool:
    """Patient or research-subject identifier (or a patient's name) in the text?"""
    t = fold(text)
    return bool(PATIENT_ID_RE.search(t) or SUBJECT_ID_RE.search(t) or PATIENT_NAME_RE.search(t))


def review_reasons(name: str, review: str, notes: str = "", exp_problem: str | None = None,
                   others: tuple = ()) -> list[str]:
    """Why a row needs a human look (empty list = it doesn't)."""
    reasons = []
    if not name or name.startswith("{") or name.lower() in ("unknown", "n/a"):
        reasons.append("unreadable scan")
    if review.lower() not in ("", "no", "no review needed", "false"):
        reasons.append("marked for review in the sheet")
    if exp_problem:
        reasons.append("unreadable expiration date")
    if any(has_patient_id(t) for t in (name, notes, *others)):
        reasons.append("contains patient identifier")
    return reasons


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

    Cells that can't be stored (numbers too big, negative quantity, ...) become
    NULL/0 and are described in the returned "problems" list.
    """
    rules = rules or {}
    problems: list[str] = []
    has_sub = any(h in row for h in ("Subcategory", "Subcategories"))
    category_cell = _first(row, "Subcategories", "Subcategory") if has_sub else clean(row.get("Category"))
    # box label: 'Box Category', or 'Main' ([AVAILABLE] tab), or 'Category' when
    # the approved category lives in a Subcategory column
    box_label_cell = _first(row, "Box Category", "Main") or (clean(row.get("Category")) if has_sub else "")
    box_cell = clean(row.get("Box Number"))

    if box_label_cell:  # dedicated box-label column + plain number
        box_type = norm_box_type(box_label_cell, rules.get("box_type"))
        box_number = bounded_int(box_cell)
    else:  # old combined label in the Box Number cell
        parsed = parse_box_label(box_cell, rules.get("box_type"))
        box_type, box_number = parsed if parsed else (None, bounded_int(box_cell))
    if box_number is None and first_int(box_cell) is not None:
        problems.append(f"Box Number {box_cell[:40]!r} too large, ignored")
    if box_type is not None and len(box_type) > 100:
        problems.append("box label longer than 100 characters, ignored")
        box_type = None

    pallet_cell = clean(row.get("Pallet Number"))
    pallet = parse_pallet(pallet_cell)
    if pallet is None and first_int(pallet_cell) is not None:
        problems.append(f"Pallet Number {pallet_cell[:40]!r} too large, ignored")

    qty_cell = clean(row.get("Quantity"))
    if is_negative(qty_cell):
        problems.append(f"negative Quantity {qty_cell[:40]!r} stored as 0")
    elif re.search(r"\d\s*[eE][+-]?\d|\d\s*[-\u2013\u2014]\s*\d", qty_cell):
        problems.append(f"Quantity {qty_cell[:40]!r} is ambiguous, read as {parse_qty(qty_cell)}")
    elif (first_int(qty_cell) or 0) > MAX_QTY:
        problems.append(f"Quantity {qty_cell[:40]!r} capped at {MAX_QTY}")

    price_cell = _first(row, "Price per unit", "Unit Value")
    price = parse_money(price_cell)
    if price is None and price_cell and re.search(r"\d", price_cell):
        problems.append(f"price {price_cell[:40]!r} too large, ignored")

    days_cell = clean(row.get("Days in Warehouse"))
    days = bounded_int(days_cell, MAX_DAYS_IN_WAREHOUSE)
    if days is None and first_int(days_cell) is not None:
        problems.append(f"Days in Warehouse {days_cell[:40]!r} too large, ignored")

    exp_problem = date_problem(row.get("Date of Expiration"))
    if exp_problem:
        problems.append(exp_problem)

    name = clean(row.get("Name"))
    review = clean(row.get("Review"))
    lot = clean(row.get("Lot Number"))
    reasons = review_reasons(name, review, clean(row.get("Notes")), exp_problem,
                             (row.get("General"), lot, row.get("Manufacturer Name"), category_cell,
                              box_label_cell, row.get("Review")))
    today = today or date.today()
    exp = parse_date(row.get("Date of Expiration"))
    wh = (today - timedelta(days=days)) if days is not None else None
    if exp and wh and exp < wh:
        reasons.append("expires before it reached the warehouse")
    if exp and exp > today.replace(year=today.year + MAX_YEARS_AHEAD, day=min(today.day, 28)):
        reasons.append(f"expiration more than {MAX_YEARS_AHEAD} years away")
    return {
        "raw_name": name,
        "general_name": clean(row.get("General")) or None,
        "category": norm_category(category_cell, rules.get("category")),
        "manufacturer": norm_manufacturer(row.get("Manufacturer Name"), rules.get("manufacturer")),
        "quantity": parse_qty(qty_cell),
        "unit_value": price,
        "lot_number": lot if lot.lower() not in ("", "unspecified", "n/a", "na") else None,
        "expiration_date": exp,
        "warehouse_date": wh,
        "box_type": box_type,
        "box_number": box_number,
        "pallet_number": pallet,
        "image_url": clean(row.get("Image URL")) or None,
        "notes": clean(row.get("Notes")) or None,
        "review": review or None,
        "flagged": bool(reasons),
        "review_reason": "; ".join(reasons) or None,
        "problems": problems,
    }


def source_key(row: dict) -> str:
    """Stable identity of a sheet row: its Image URL (each scan has its own
    photo). Rows without one fall back to a hash of what's on the label."""
    url = clean(row.get("Image URL"))
    if url:
        return url
    parts = [clean(row.get(h)) for h in ("Name", "Manufacturer Name", "Lot Number",
                                         "Date of Expiration", "General", "Notes")]
    return "hash:" + _md5("\x1f".join(parts))


def content_hash(row: dict) -> str:
    """Hash of the cells only (not the row position)."""
    return _md5(json.dumps(row, sort_keys=True, ensure_ascii=False))


# Bump when the normalizing rules change in a way that should re-process every
# row on the next sync (e.g. a new review reason). Costs one full update run.
RULES_VERSION = 3


def row_hash(row: dict, sheet_row: int | None) -> str:
    """Changes whenever any cell (or the row's position, or RULES_VERSION) changes."""
    payload = json.dumps({"row": row, "sheet_row": sheet_row, "rules": RULES_VERSION},
                         sort_keys=True, ensure_ascii=False)
    return _md5(payload)
