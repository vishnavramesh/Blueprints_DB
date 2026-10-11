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


def qty_ambiguous(raw) -> bool:
    """'1e3' or '5-10': we read the first number, but the cell may mean something else."""
    return bool(re.search(r"\d\s*[eE][+-]?\d|\d\s*[-\u2013\u2014]\s*\d", clean(raw)))


def money_ambiguous(raw) -> bool:
    """'1.234' / '1,234': a thousands separator or a decimal point? (we read one way)"""
    return bool(re.fullmatch(r"\D*\d{1,3}[.,]\d{3}\D*", clean(raw)))


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
_ID_TOKEN = r"(?:(?!ID\b)[A-Z]{1,3}\b(?![\'\u2019-]|\.?\s+[A-Z][a-z])|[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*)"
PATIENT_ID_RE = re.compile(
    r"\b(?:[Pp][Tt]\b\.?|[Pp][Aa][Tt][Ii][Ee][Nn][Tt]\b)"
    r"(?:\s*(?:[Ii][Dd]\b|#|[Nn][Oo]\b\.?))?[\s:#,.]*"
    + _ID_TOKEN + r"(?:[\s,]+" + _ID_TOKEN + r"){0,3}")


# Research-subject / specimen ID on trial sample tubes ("A00-000", "a00 - 000").
# Mirrors b4p_subject_id_pattern() in sql/schema.sql.
SUBJECT_ID_PATTERN = r"\b[A-Za-z][0-9]{2}\s*-\s*[0-9]{3}\b"
SUBJECT_ID_RE = re.compile(SUBJECT_ID_PATTERN)

# Letters of any common script (written out so it does not depend on the database locale)
_L = r"A-Za-zÀ-ɏͰ-ϿЀ-ӿḀ-ỿ"
_T = r"[" + _L + r"][" + _L + r"'\u2019.-]*"        # one name token: José, O'Brien, J., Smith-Jones

# Ordinary supply words that follow "Patient" in product names ("Patient Gait Belt").
# They are never taken for a surname. Matching is case-insensitive; a plural -s/-es is allowed.
_STOP_WORDS = (
    "id|name|return|starter|slipper|use|used|monitoring|monitor|disposable|reusable|single|sterile|belonging|"
    "lift|lifter|lifting|transfer|care|controlled|warming|warmer|gown|bed|safety|restraint|positioning|"
    "positioner|wedge|education|comfort|handling|ready|preoperative|preparation|specimen|sample|identification|"
    "information|wristband|armband|bracelet|band|wipe|isolation|support|cart|chair|hygiene|bathing|bath|skin|"
    "cleanser|slide|sheet|sling|aid|kit|supply|supplies|access|contact|cooling|heating|side|port|cable|circuit|"
    "interface|sitter|cuff|line|hose|tubing|mask|wound|dressing|glove|drape|cover|pillow|blanket|bag|cap|label|"
    "chart|record|data|room|left|right|instruction|tray|scale|thermometer|electrode|hoist|board|belt|shoe|sock|"
    "brief|diaper|underpad|pad|urinal|bedpan|commode|walker|wheelchair|stretcher|mattress|towel|washcloth|"
    "cloth|wash|hand|basin|item|product|set|pack|call|cord|alarm|garment|scrub|cushion|table|folder|transport|"
    "oxygen|suction|canister|gait|fall|mat|protection|turn|assist|valuable|pressure|simulator|gauze|needle|"
    "syringe|bandage|tape|hearing|hair|bib|temperature|probe|sensor|lead|system|pump|analgesia|catheter|"
    "bottle|container|cup|pitcher|tube|valve|filter|adapter|connector|clip|strap|harness|vest|garment|sleeve|"
    "stocking|boot|splint|brace|collar|rail|pole|stand|lamp|light|camera|phone|remote|control|button|switch|"
    "screen|display|software|program|plan|form|sticker|stamp|marker|pen|pencil|paper|envelope|file|binder|"
    "stool|charting|holder|privacy|weighing|compression|positioning|prep|device|lateral|resuscitator|vest|"
    "skin|safety|satisfaction|survey|bill|rights|portal|identification|armband")
_STOP = r"(?:" + _STOP_WORDS + r")(?:e?s)?"
_NAMEWORD = r"[" + _L + r"][" + _L + r"'\u2019-]*"

def _ci(pattern: str) -> str:
    """Make the letters of a simple word pattern case-insensitive without (?i), so a pattern can
    mix case-insensitive words with a case-sensitive keyword (Postgres has no (?i:...) groups)."""
    return re.sub(r"[A-Za-z]", lambda m: f"[{m.group().upper()}{m.group().lower()}]", pattern)


_STOP_CI = _ci(_STOP)

# After an explicit label ("Patient:", "Pt. Name:", or a dash) whatever comes next IS the name:
# the next one to three words, in any script. After a plain dash a product word doesn't count.
# Mirrors b4p_patient_name_pattern() in sql/schema.sql.
_ANYWORDS = r"\S+(?:\s+\S+){0,2}"
PATIENT_NAME_PATTERN = (r"(?i)(?<![0-9])(?<![0-9]\s)\b(?:pt\b\.?|patient\b)(?:\s+name)?\s*"
                        r"(?::\s*(?!id\b)" + _ANYWORDS + r"|-\s*(?!(?:" + _STOP + r"|id)\b)" + _ANYWORDS + r")")
PATIENT_NAME_RE = re.compile(PATIENT_NAME_PATTERN)

# A name straight after Patient / Pt. with no label ("Patient Smith bed 3", "Pt J. Smith",
# "Pt van der Berg", "Patient 山田 太郎"): up to three capitalised words (initials and
# particles allowed) whatever follows, unless the first word is an ordinary supply word
# ("Patient Gait Belt"). A lower-case name ("pt. smith") only counts at the end of the text, before a
# comma, DOB/MRN/ID or a number. Lower-case "pt" needs its period ("12 pt font" is a size); never after a digit.
# Mirrors b4p_patient_surname_pattern() in sql/schema.sql.
_PT_KEYWORD = r"(?:" + _ci("patient") + r"\b|(?:Pt|PT)\b\.?|pt\b\.)"
_CJK = r"぀-ヿ㐀-鿿가-힯"
_UPPER = r"A-ZÀ-ÞΑ-ΩА-Я" + _CJK
_CAPWORD = r"[" + _UPPER + r"][" + _L + _CJK + r"'\u2019/.-]*"
_PARTICLE = _ci("(?:de|la|del|della|van|der|den|von|da|di|le|du|ter|al|el|bin|ibn)")
_NOSTOP = r"(?!" + _STOP_CI + r"\b)"
_LOWWORD = r"[" + _L + r"'\u2019-]+(?![" + _L + r"0-9])"
PATIENT_SURNAME_PATTERN = (
    r"(?<![0-9])(?<![0-9]\s)\b" + _PT_KEYWORD + r"\s+(?:"
    r"(?:" + _PARTICLE + r"\s+){0,2}" + _NOSTOP + _CAPWORD
    + r"(?:\s+(?:" + _PARTICLE + r"\s+){0,2}" + _NOSTOP + _CAPWORD + r"){0,2}"
    + r"|" + _NOSTOP + _LOWWORD + r"(?:\s+" + _NOSTOP + _LOWWORD + r")?"
    + r"(?=\.?\s*$|\s*[,;]|\s+(?:" + _ci("dob") + "|" + _ci("mrn") + "|" + _ci("id") + r")\b|\s*[0-9]))")
PATIENT_SURNAME_RE = re.compile(PATIENT_SURNAME_PATTERN)

# Date of birth: numeric, "March 3 1980" or "3rd March 1980", after DOB / born (on)
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DOB_PATTERN = (r"(?i)\b(?:dob|d\.o\.b\.?|date of birth|born(?:\s+on)?)[\s:]*(?:[0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4}"
               r"|" + _MON + r"\s+[0-9]{1,2}(?:st|nd|rd|th)?,?\s+[0-9]{2,4}"
               r"|[0-9]{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MON + r",?\s+[0-9]{2,4})")
DOB_RE = re.compile(DOB_PATTERN)
SSN_PATTERN = r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b"
SSN_RE = re.compile(SSN_PATTERN)

# Zero-width / bidi / soft-hyphen characters people (or scanners) slip into text
_INVISIBLE_RE = re.compile("[­؜​-‏‪-‮⁠-⁤⁦-⁩﻿]")
# Dash look-alikes -> '-', and Cyrillic/Greek letters that look like Latin ones -> Latin
_DASH_FROM = "‐‑‒–—―−"
_HOMOGLYPHS = {  # Cyrillic and Greek letters -> the Latin letter they look like
    "\u0430\u0435\u043e\u0440\u0441\u0445\u0443\u0456\u0458\u0455\u043a\u043c\u0442\u043d\u0432": "aeopcxyijskmtnb",
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425\u0423\u0406\u0408\u0405": "ABEKMHOPCTXYIJS",
    "\u03bf\u03c1\u03bd\u03c4\u03b1\u03b5\u03b9\u03ba\u03c5\u03c7": "opvtaeikux",
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7": "ABEZHIKMNOPTYX",
}
HOMOGLYPH_FROM = "".join(_HOMOGLYPHS)
HOMOGLYPH_TO = "".join(_HOMOGLYPHS.values())
assert len(HOMOGLYPH_FROM) == len(HOMOGLYPH_TO)
_FOLD_TABLE = str.maketrans(_DASH_FROM + HOMOGLYPH_FROM, "-" * len(_DASH_FROM) + HOMOGLYPH_TO)


def fold(text) -> str:
    """Text as the redaction patterns should see it: no invisible characters,
    full-width / compatibility forms folded (NFKC), dash and letter look-alikes
    turned into plain ASCII. Mirrors b4p_fold()."""
    return unicodedata.normalize("NFKC", _INVISIBLE_RE.sub("", clean(text))).translate(_FOLD_TABLE)


def has_patient_id(text) -> bool:
    """Patient or research-subject identifier (or a patient's name) in the text?"""
    t = fold(text)
    return any(r.search(t) for r in (PATIENT_ID_RE, SUBJECT_ID_RE, PATIENT_NAME_RE, PATIENT_SURNAME_RE,
                                     DOB_RE, SSN_RE))


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
    elif qty_ambiguous(qty_cell):
        problems.append(f"Quantity {qty_cell[:40]!r} is ambiguous, read as {parse_qty(qty_cell)}")
    elif (first_int(qty_cell) or 0) > MAX_QTY:
        problems.append(f"Quantity {qty_cell[:40]!r} capped at {MAX_QTY}")

    price_cell = _first(row, "Price per unit", "Unit Value")
    price = parse_money(price_cell)
    if price is None and price_cell and re.search(r"\d", price_cell):
        problems.append(f"price {price_cell[:40]!r} too large, ignored")
    elif price is not None and money_ambiguous(price_cell):
        problems.append(f"price {price_cell[:40]!r} is ambiguous (decimal point or thousands?), read as {price:g}")

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
