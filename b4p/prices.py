"""Unit prices for supplies (`python -m b4p prices`).

Sources, cheapest first; each one only fills what is still missing:
  * sheet:   the inventory spreadsheet's "w/ pricing" tab has a "Per-Unit Price"
             per row. A priced row is linked to a supply by, in order:
               1. exact normalized name + manufacturer          -> applied
               2. Image URL (the same photo as an inventory row) -> applied
               3. pg_trgm similarity >= threshold with the same (known)
                  manufacturer and the same distinguishing tokens (numbers,
                  units, sizes, sterile/non-sterile, latex/latex-free, male/
                  female, colours, case/box/pack ...)  -> applied only if the
                  similarity is >= AUTO_APPLY_SCORE, else 'suggested'
             Several rows for one supply -> their median, unless the prices
             differ by more than 3x ('conflict', not applied).
  * f25-api: optional; unpriced supplies are sent to the B4P Pricing Enrichment
             API (POST /v1/enrich/batch). Only confident 'priced' answers are kept.
  * manual:  `python -m b4p unpriced` writes a review CSV (with the suggestions
             and conflicts); a person fills in prices and imports it. Any
             direct UPDATE of supply.unit_value_usd also counts as manual.

supply_price records every price, suggestion and conflict with its source.
Automatic sources never touch a supply that has a manual price or a manual
"do not auto-price" marker.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import re
import statistics
import time
from collections import defaultdict

import httpx
import psycopg

from b4p import normalize as N

log = logging.getLogger("b4p.prices")

SHEET, API, MANUAL = "sheet", "f25-api", "manual"
PRICE_HEADER = "Per-Unit Price"
DEFAULT_THRESHOLD = 0.8      # fuzzy matches below this are ignored
AUTO_APPLY_SCORE = 0.95      # fuzzy matches below this are only suggested
MAX_SPREAD = 3.0             # matched prices differing more than this -> conflict
DEFAULT_MAX_PRICE = 10_000.0  # anything above is almost surely a typo (e.g. $119,900 shoe covers)
API_DEADLINE = 120.0         # seconds for the whole API step
API_MAX_RESPONSE = 5_000_000  # bytes per response

# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
_MARKS = re.compile(r"[™®©*{}\"“”]")
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z]+")
_SYNONYMS = {"pk": "pack", "pks": "pack", "pkg": "pack", "packs": "pack", "cs": "case", "cases": "case",
             "bx": "box", "boxes": "box", "bags": "bag", "ga": "g", "gauge": "g", "cc": "ml",
             "gray": "grey", "paediatric": "pediatric", "peds": "pediatric", "neonates": "neonate",
             "neonatal": "neonate", "lg": "large", "med": "medium", "sm": "small", "lt": "left",
             "rt": "right", "nonsterile": "non", "unsterile": "non", "latexfree": "free"}
DISTINGUISHING = {
    "non", "free", "male", "female", "sterile", "latex", "case", "box", "pack", "bag", "each",
    "red", "blue", "green", "yellow", "purple", "white", "black", "orange", "pink", "clear", "grey",
    "brown", "teal", "violet", "amber", "tan", "lavender", "gold",
    "adult", "pediatric", "infant", "child", "neonate", "toddler", "newborn", "left", "right",
    "xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "small", "medium", "large", "extra", "regular",
    "ml", "mm", "cm", "g", "mg", "kg", "mcg", "fr", "oz", "ft", "yd", "yds", "lb", "french",
}


def norm_name(raw) -> str:
    """'BD Insyte™ Autoguard™  22 GA' -> 'bd insyte autoguard 22 ga'."""
    text = _MARKS.sub(" ", N.clean(raw).translate(N._DASHES).lower())
    return re.sub(r"\s+", " ", text).strip()


def signature(name: str) -> frozenset[str]:
    """The tokens two names must share exactly to be fuzzy-matched: every
    number and every distinguishing word ('10ml' -> {'10', 'ml'})."""
    text = re.sub(r"(\d),(\d{1,2})(?!\d)", r"\1.\2", name)   # 5,0 cm -> 5.0 cm
    text = re.sub(r"(\d),(\d{3})", r"\1\2", text)             # 1,000 -> 1000
    out = set()
    for tok in _TOKEN.findall(text):
        if tok[0].isdigit():
            out.add(f"{float(tok):g}")
        else:
            tok = _SYNONYMS.get(tok, tok)
            if tok in DISTINGUISHING:
                out.add(tok)
    return frozenset(out)


def mfr_key(raw, aliases: dict[str, str]) -> str:
    """Canonical lower-case manufacturer, '' if unknown."""
    return (N.norm_manufacturer(N.clean(raw).strip("\"' "), aliases) or "").lower()


# ---------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------
_PRICE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d{1,7})(\.\d{1,10})?")  # long decimals = box price / count


def parse_price(cell, max_price: float = DEFAULT_MAX_PRICE) -> tuple[float | None, str | None]:
    """'$1,234.50' -> (1234.5, None). Blank -> (None, None). Anything else
    (negative, '(5.00)', '1e9', '€5,50', '12 per box of 100', 0, > max_price)
    -> (None, reason)."""
    text = N.clean(cell)
    if not text:
        return None, None
    m = _PRICE.fullmatch(text)
    if not m:
        return None, "not a plain dollar amount"
    value = float(m.group(1).replace(",", "") + (m.group(2) or ""))
    if value <= 0:
        return None, "zero"
    if value > max_price:
        return None, f"above max price {max_price:g}"
    return value, None


# ---------------------------------------------------------------------------
# sheet prices
# ---------------------------------------------------------------------------
def read_priced_rows(rows: list[tuple[int, dict]], aliases: dict[str, str],
                     max_price: float = DEFAULT_MAX_PRICE) -> tuple[list[dict], list[tuple]]:
    """Sheet rows -> (usable priced rows, rejected [(sheet_row, cell, reason)]).
    Rows with a blank price or an unusable name are skipped silently."""
    out, rejected = [], []
    for sheet_row, row in rows:
        name = norm_name(row.get("Name"))
        price, problem = parse_price(row.get(PRICE_HEADER), max_price)
        if problem:
            rejected.append((sheet_row, N.clean(row.get(PRICE_HEADER)), problem))
        elif price is not None and len(name) >= 3 and name not in ("unknown", "n/a"):
            out.append({"row": sheet_row, "name": N.clean(row.get("Name")), "key": name,
                        "mfr": mfr_key(row.get("Manufacturer Name"), aliases),
                        "image_url": N.clean(row.get("Image URL")), "price": price})
    return out, rejected


def _decide(rows: list[dict], matched_by: str, score: float) -> dict:
    """Matched priced rows -> one match record (status applied/suggested/conflict)."""
    values = [r["price"] for r in rows]
    detail = f"row {rows[0]['row']}: {rows[0]['name'][:200]}"
    if len(rows) > 1:
        detail += f" (+{len(rows) - 1} more rows)"
    match = {"price": round(statistics.median(values), 2), "matched_by": matched_by, "score": score,
             "detail": detail, "status": "applied"}
    if max(values) > MAX_SPREAD * min(values):
        match.update(price=None, status="conflict",
                     detail=detail + "; prices " + ", ".join(f"{v:g}" for v in sorted(set(values))))
    elif matched_by == "trgm" and score < AUTO_APPLY_SCORE:
        match["status"] = "suggested"
    elif match["price"] <= 0:
        match.update(price=None, status="conflict", detail=detail + "; rounds to $0.00")
    return match


def match_sheet(cur, priced: list[dict], threshold: float = DEFAULT_THRESHOLD) -> dict[int, dict]:
    """{supply_id: match} for every supply a priced row matches; a match is
    {price, matched_by, score, detail, status}."""
    cur.execute("SELECT s.supply_id, s.name, lower(m.name) FROM supply s "
                "LEFT JOIN manufacturer m ON m.manufacturer_id = s.manufacturer_id")
    supplies = [(sid, norm_name(name), mfr or "") for sid, name, mfr in cur.fetchall()]
    hits: dict[int, tuple] = {}

    # 1. exact normalized name + manufacturer
    by_key = defaultdict(list)
    for p in priced:
        by_key[(p["key"], p["mfr"])].append(p)
    for sid, key, mfr in supplies:
        if (key, mfr) in by_key:
            hits[sid] = (by_key[(key, mfr)], "name+manufacturer", 1.0)

    # 2. same photo as one of our inventory rows
    cur.execute("SELECT image_url, supply_id FROM inventory_item "
                "WHERE supply_id IS NOT NULL AND COALESCE(image_url, '') <> ''")
    by_image = dict(cur.fetchall())
    image_hits = defaultdict(list)
    for p in priced:
        sid = by_image.get(p["image_url"])
        if sid is not None and sid not in hits:
            image_hits[sid].append(p)
    hits.update({sid: (rows, "image_url", 1.0) for sid, rows in image_hits.items()})

    # 3. trigram similarity: same known manufacturer, same distinguishing tokens
    rest = [s for s in supplies if s[0] not in hits and len(s[1]) >= 3 and s[2]]
    fuzzy_priced = [(i, p) for i, p in enumerate(priced) if p["mfr"]]
    if rest and fuzzy_priced:
        cur.execute("DROP TABLE IF EXISTS _price_sp, _price_pr")
        cur.execute("CREATE TEMP TABLE _price_sp (supply_id BIGINT, name TEXT, mfr TEXT) ON COMMIT DROP")
        cur.execute("CREATE TEMP TABLE _price_pr (i INT, name TEXT, mfr TEXT) ON COMMIT DROP")
        with cur.copy("COPY _price_sp FROM STDIN") as cp:
            for row in rest:
                cp.write_row(row)
        with cur.copy("COPY _price_pr FROM STDIN") as cp:
            for i, p in fuzzy_priced:
                cp.write_row((i, p["key"], p["mfr"]))
        cur.execute("SELECT s.supply_id, s.name, p.i, p.name, similarity(s.name, p.name) AS sim "
                    "FROM _price_sp s JOIN _price_pr p ON p.mfr = s.mfr "
                    "AND similarity(s.name, p.name) >= %s ORDER BY s.supply_id, sim DESC", (threshold,))
        best = {}
        for sid, sname, i, pname, sim in cur.fetchall():
            if signature(sname) != signature(pname):
                continue
            sim = round(float(sim), 3)
            if sid not in best:
                best[sid] = (sim, [priced[i]])
            elif sim == best[sid][0]:
                best[sid][1].append(priced[i])
        hits.update({sid: (rows, "trgm", sim) for sid, (sim, rows) in best.items()})

    return {sid: _decide(rows, how, score) for sid, (rows, how, score) in hits.items()}


# ---------------------------------------------------------------------------
# writing prices
# ---------------------------------------------------------------------------
# Automatic sources may write a supply's price only if nobody pinned it by hand
# and it is empty or was itself set by an automatic source.
_AUTO_ALLOWED = """
NOT EXISTS (SELECT 1 FROM supply_price p WHERE p.supply_id = s.supply_id AND p.source = 'manual')
AND (s.unit_value_usd IS NULL OR EXISTS (
     SELECT 1 FROM supply_price p WHERE p.supply_id = s.supply_id AND p.source <> 'manual'
        AND p.status = 'applied' AND p.price = s.unit_value_usd))"""

_RECORD = """
INSERT INTO supply_price (supply_id, source, status, price, matched_by, score, detail, fetched_at)
VALUES (%(sid)s, %(source)s, %(status)s, %(price)s, %(matched_by)s, %(score)s, %(detail)s, clock_timestamp())
ON CONFLICT (supply_id, source) DO UPDATE
   SET status = EXCLUDED.status, price = EXCLUDED.price, matched_by = EXCLUDED.matched_by,
       score = EXCLUDED.score, detail = EXCLUDED.detail, fetched_at = EXCLUDED.fetched_at"""


def _apply_one(cur, sid: int, m: dict, source: str) -> str:
    args = {"sid": sid, "source": source, **m}
    cur.execute("SELECT EXISTS (SELECT 1 FROM supply s WHERE s.supply_id = %s), "
                f"EXISTS (SELECT 1 FROM supply s WHERE s.supply_id = %s AND {_AUTO_ALLOWED})", (sid, sid))
    exists, allowed = cur.fetchone()
    if not exists:
        return "unknown"
    if source != MANUAL and not allowed:
        return "kept_manual"
    if m["status"] in ("applied", "blocked"):
        cur.execute("UPDATE supply SET unit_value_usd = %s WHERE supply_id = %s", (m["price"], sid))
    else:  # no longer a confident match: withdraw the price this source set earlier
        _withdraw(cur, source, "s.supply_id = %(sid)s", {"sid": sid})
    cur.execute(_RECORD, args)
    return {"applied": "set", "blocked": "blocked"}.get(m["status"], m["status"])


def _withdraw(cur, source: str, where: str, args: dict) -> int:
    """Clear supply prices that `source` set and that are still unchanged and
    not pinned by hand."""
    cur.execute(f"""
        UPDATE supply s SET unit_value_usd = NULL FROM supply_price p
         WHERE p.supply_id = s.supply_id AND p.source = %(source)s AND p.status = 'applied'
           AND p.price = s.unit_value_usd AND {where}
           AND NOT EXISTS (SELECT 1 FROM supply_price q WHERE q.supply_id = s.supply_id AND q.source = 'manual')""",
                {"source": source, **args})
    return cur.rowcount


def apply_prices(cur, matches: dict[int, dict], source: str) -> dict:
    """Write {supply_id: match}. Each supply is written in its own savepoint,
    so one bad value cannot undo the others. Returns counts per outcome
    (set, suggested, conflict, blocked, kept_manual, unknown, error, withdrawn).
    For the sheet this is a full refresh: prices it set earlier for supplies
    it no longer matches are withdrawn."""
    stats = defaultdict(int)
    cur.execute("SELECT set_config('b4p.price_writer', 'prices', true)")
    try:
        if source == SHEET:
            stats["withdrawn"] = _withdraw(cur, source, "s.supply_id <> ALL(%(keep)s)", {"keep": list(matches)})
            cur.execute("DELETE FROM supply_price WHERE source = %s AND (status <> 'applied' "
                        "OR supply_id <> ALL(%s))", (source, list(matches)))
        for sid, m in matches.items():
            try:
                with cur.connection.transaction():
                    stats[_apply_one(cur, sid, m, source)] += 1
            except psycopg.Error as e:
                stats["error"] += 1
                log.error("price for supply %s not saved: %s", sid, str(e).splitlines()[0][:150])
    finally:
        cur.execute("SELECT set_config('b4p.price_writer', '', true)")
    return dict(stats)


# ---------------------------------------------------------------------------
# B4P Pricing Enrichment API (f25_b4p_tech_project)
# ---------------------------------------------------------------------------
def _number(v) -> float | None:
    """A finite int/float (not bool), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _api_match(res, sent_ids: set[int], max_price: float) -> tuple[int, dict] | None:
    """One EnrichedItem -> (supply_id, match) if it is a usable, confident price."""
    if not isinstance(res, dict):
        return None
    ext = res.get("external_id")
    sid = int(ext) if isinstance(ext, str) and ext.isdigit() else None
    price = _number(res.get("unit_price"))
    conf = res.get("confidence")
    if (sid not in sent_ids or res.get("status") != "priced" or res.get("needs_review") is not False
            or res.get("currency") != "USD" or price is None or not 0 < price <= max_price
            or round(price, 2) <= 0 or (conf is not None and _number(conf) is None)):
        return None
    detail = " ".join(str(res.get(k) or "")[:250] for k in ("matched_item", "price_url")).strip()
    source = re.sub(r"[^a-z_-]", "", str(res.get("source") or "unknown").lower())[:30] or "unknown"
    return sid, {"price": round(price, 2), "matched_by": f"f25:{source}", "score": _number(conf),
                 "detail": detail or None, "status": "applied"}


def enrich_via_api(cur, url: str, api_key: str, limit: int = 100, batch_size: int = 25,
                   max_price: float = DEFAULT_MAX_PRICE, client: httpx.Client | None = None,
                   deadline: float = API_DEADLINE) -> tuple[dict, dict]:
    """Send up to `limit` unpriced supplies (most units first, never ones marked
    "do not auto-price") to POST <url>/v1/enrich/batch. Returns
    ({supply_id: match}, stats). Keeps an answer only if status is 'priced',
    needs_review is false, currency is 'USD', unit_price is a number in
    (0, max_price] and confidence is a number or null. Stops at the first HTTP
    error, an oversized/invalid response, or after `deadline` seconds."""
    cur.execute("SELECT u.supply_id, u.name, COALESCE(u.manufacturer, ''), u.category, "
                "COALESCE(u.general_name, ''), u.units, COALESCE(u.image_url, '') FROM v_unpriced u "
                "WHERE NOT EXISTS (SELECT 1 FROM supply_price p WHERE p.supply_id = u.supply_id "
                "AND p.source = 'manual') ORDER BY u.units DESC, u.supply_id LIMIT %s", (limit,))
    items = [{"external_id": str(sid), "name": name, "manufacturer_name": mfr, "category": cat,
              "subcategories": cat, "general": gen, "quantity": int(units), "image_url": img}
             for sid, name, mfr, cat, gen, units, img in cur.fetchall()]
    stats = {"sent": 0, "priced": 0, "rejected": 0, "cost_usd": 0.0, "error": None}
    matches = {}
    own_client = client is None
    client = client or httpx.Client()
    endpoint = url.rstrip("/") + "/v1/enrich/batch"
    stop_at = time.monotonic() + deadline
    try:
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            try:
                body = _post(client, endpoint, api_key, batch, stop_at)
            except (httpx.HTTPError, ValueError) as e:
                first_line = (str(e).splitlines() or [""])[0]
                stats["error"] = f"{endpoint}: {type(e).__name__} {first_line[:150]}"
                log.error("pricing API: %s", stats["error"])
                break
            stats["sent"] += len(batch)
            stats["cost_usd"] += _number((body.get("usage") or {}).get("cost_usd")) or 0.0
            sent_ids = {int(i["external_id"]) for i in batch}
            for res in body["items"]:
                hit = _api_match(res, sent_ids, max_price)
                if hit is None:
                    stats["rejected"] += 1
                else:
                    matches[hit[0]] = hit[1]
                    stats["priced"] += 1
    finally:
        if own_client:
            client.close()
    return matches, stats


def _post(client: httpx.Client, endpoint: str, api_key: str, batch: list, stop_at: float) -> dict:
    remaining = stop_at - time.monotonic()
    if remaining <= 0:
        raise ValueError("deadline reached")
    with client.stream("POST", endpoint, json={"items": batch}, timeout=min(remaining, 60),
                       headers={"Authorization": f"Bearer {api_key}"}) as r:
        r.raise_for_status()  # also refuses redirects
        data = bytearray()
        for chunk in r.iter_bytes():
            data += chunk
            if len(data) > API_MAX_RESPONSE:
                raise ValueError(f"response larger than {API_MAX_RESPONSE} bytes")
            if time.monotonic() > stop_at:
                raise ValueError("deadline reached")
    body = json.loads(data)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise ValueError("response has no 'items' list")
    if not isinstance(body.get("usage") or {}, dict):
        raise ValueError("response 'usage' is not an object")
    return body


# ---------------------------------------------------------------------------
# review CSV for humans
# ---------------------------------------------------------------------------
EXPORT_COLUMNS = ["supply_id", "name", "manufacturer", "category", "general_name", "active_rows", "units",
                  "image_url", "suggested_price", "suggestion", "unit_value_usd", "no_auto_price"]
_YES = {"y", "yes", "x", "1", "true"}


def _csv_safe(v):
    """Neutralize spreadsheet formulas in exported text."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def export_unpriced(cur, f) -> int:
    """v_unpriced as a review CSV, most units first: suggested matches and
    conflicts from the pricing tab, and empty unit_value_usd / no_auto_price
    columns to fill in."""
    cur.execute("""
        SELECT u.supply_id, u.name, u.manufacturer, u.category, u.general_name, u.active_rows, u.units,
               u.image_url, sg.price,
               CASE WHEN sg.status IS NULL THEN NULL
                    ELSE sg.status || ' (' || sg.matched_by || COALESCE(' ' || round(sg.score::numeric, 2), '')
                         || '): ' || COALESCE(sg.detail, '') END,
               NULL, CASE WHEN bl.supply_id IS NOT NULL THEN 'yes' END
          FROM v_unpriced u
          LEFT JOIN supply_price sg ON sg.supply_id = u.supply_id AND sg.source <> 'manual'
                                   AND sg.status IN ('suggested', 'conflict')
          LEFT JOIN supply_price bl ON bl.supply_id = u.supply_id AND bl.source = 'manual'
                                   AND bl.status = 'blocked'
         ORDER BY u.units DESC, u.supply_id""")
    w = csv.writer(f)
    w.writerow(EXPORT_COLUMNS)
    n = 0
    for row in cur.fetchall():
        w.writerow([_csv_safe(v) for v in row])
        n += 1
    return n


def read_manual_csv(f, max_price: float = DEFAULT_MAX_PRICE) -> tuple[dict[int, dict], list[str]]:
    """Filled-in review CSV -> ({supply_id: match}, problems). A row with a
    price sets it; a row with no price but no_auto_price = yes blocks the
    automatic sources (and clears their price); other rows are skipped."""
    matches, problems = {}, []
    for line, row in enumerate(csv.DictReader(f), start=2):
        cell = (row.get("unit_value_usd") or "").strip()
        block = (row.get("no_auto_price") or "").strip().lower() in _YES
        if not cell and not block:
            continue
        sid = (row.get("supply_id") or "").strip()
        if not sid.isdigit():
            problems.append(f"line {line}: bad supply_id {sid!r}")
            continue
        if not cell:
            matches[int(sid)] = {"price": None, "matched_by": "csv", "score": None, "detail": None,
                                 "status": "blocked"}
            continue
        price, problem = parse_price(cell, max_price)
        if problem:
            problems.append(f"line {line}: price {cell[:40]!r} rejected ({problem})")
            continue
        matches[int(sid)] = {"price": round(price, 2), "matched_by": "csv", "score": None,
                             "detail": None, "status": "applied"}
    return matches, problems


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def rejected_summary(rejected: list[tuple]) -> list[str]:
    """[(row, cell, reason)] -> one line per distinct cell text, listing every row."""
    groups = defaultdict(list)
    for row, cell, reason in rejected:
        groups[(cell, reason)].append(row)
    return [f"{cell[:40]!r} ({reason}) x{len(rows)}: rows {', '.join(map(str, rows))}"
            for (cell, reason), rows in sorted(groups.items(), key=lambda g: -len(g[1]))]


def coverage(cur) -> str:
    cur.execute("""
        SELECT count(DISTINCT i.supply_id) FILTER (WHERE s.unit_value_usd IS NOT NULL),
               count(DISTINCT i.supply_id),
               count(*) FILTER (WHERE COALESCE(i.unit_value, s.unit_value_usd) IS NOT NULL), count(*),
               COALESCE(sum(i.quantity) FILTER (WHERE COALESCE(i.unit_value, s.unit_value_usd) IS NOT NULL), 0),
               COALESCE(sum(i.quantity), 0),
               COALESCE(sum(i.quantity * COALESCE(i.unit_value, s.unit_value_usd, 0)), 0)
          FROM inventory_item i LEFT JOIN supply s ON s.supply_id = i.supply_id
         WHERE i.removed_at IS NULL""")
    ps, ts, pr, tr, pu, tu, value = cur.fetchone()
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "-"
    return (f"priced: {ps}/{ts} supplies on the shelf ({pct(ps, ts)}), {pr}/{tr} rows ({pct(pr, tr)}), "
            f"{pu}/{tu} units ({pct(pu, tu)}); total value ${value:,.2f}")
