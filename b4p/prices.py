"""Unit prices for supplies (`python -m b4p prices`).

Sources, cheapest first; each one only fills what is still missing:
  * sheet:   the inventory spreadsheet's "w/ pricing" tab has a "Per-Unit Price"
             per row. A priced row is linked to a supply by, in order:
               1. exact normalized name + manufacturer,
               2. Image URL (the same photo as one of our inventory rows),
               3. pg_trgm similarity >= threshold, the same manufacturer when
                  both are known, and the same numbers and size words in both
                  names ('10 mL' never matches '5 mL', 'Medium' never 'Large').
             Several priced rows for one supply -> their median.
  * f25-api: optional; unpriced supplies are sent to the B4P Pricing Enrichment
             API (POST /v1/enrich/batch). Only confident 'priced' answers are kept.
  * manual:  a CSV from `python -m b4p unpriced`, filled in by a person.

supply_price records where each price came from. A price that was not set by
the sheet or the API (a manual import, or someone's UPDATE) is never
overwritten by them.
"""
from __future__ import annotations

import csv
import logging
import re
import statistics
from collections import defaultdict

import httpx

from b4p import normalize as N

log = logging.getLogger("b4p.prices")

SHEET, API, MANUAL = "sheet", "f25-api", "manual"
PRICE_HEADER = "Per-Unit Price"
DEFAULT_THRESHOLD = 0.8
DEFAULT_MAX_PRICE = 10_000.0  # anything above is almost surely a typo (e.g. $119,900 shoe covers)

_MARKS = re.compile(r"[™®©*{}\"“”]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
SIZE_WORDS = {"xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "small", "medium", "large",
              "x-large", "xx-large", "extra-large", "regular", "adult", "pediatric", "paediatric",
              "child", "infant", "neonatal", "neonate", "neonates", "toddler", "newborn"}


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
def norm_name(raw) -> str:
    """'BD Insyte™ Autoguard™  22 GA' -> 'bd insyte autoguard 22 ga'."""
    text = _MARKS.sub(" ", N.clean(raw).translate(N._DASHES).lower())
    return re.sub(r"\s+", " ", text).strip()


def signature(name: str) -> tuple[list[str], list[str]]:
    """Numbers and size words in a normalized name; two names may only be
    fuzzy-matched when these are equal."""
    nums = sorted(re.sub(r"^0+(?=\d)", "", n.replace(",", ".")) for n in _NUMBER.findall(name))
    sizes = sorted(set(re.findall(r"[a-z]+(?:-[a-z]+)*", name)) & SIZE_WORDS)
    return nums, sizes


def mfr_key(raw, aliases: dict[str, str]) -> str:
    """Canonical lower-case manufacturer, '' if unknown."""
    return (N.norm_manufacturer(N.clean(raw).strip("\"' "), aliases) or "").lower()


# ---------------------------------------------------------------------------
# sheet prices
# ---------------------------------------------------------------------------
def read_priced_rows(rows: list[tuple[int, dict]], aliases: dict[str, str],
                     max_price: float = DEFAULT_MAX_PRICE) -> tuple[list[dict], dict]:
    """Sheet rows -> usable priced rows, plus counts of what was skipped."""
    out, skipped = [], defaultdict(int)
    for sheet_row, row in rows:
        name = norm_name(row.get("Name"))
        price = N.parse_money(row.get(PRICE_HEADER))
        if len(name) < 3 or name in ("unknown", "n/a"):
            skipped["no usable name"] += 1
        elif price is None or price <= 0:
            skipped["no price"] += 1
        elif price > max_price:
            skipped[f"price above {max_price:g}"] += 1
            log.warning("pricing tab row %s: %r at %s skipped (above --max-price)",
                        sheet_row, N.clean(row.get("Name"))[:60], price)
        else:
            out.append({"row": sheet_row, "name": N.clean(row.get("Name")), "key": name,
                        "mfr": mfr_key(row.get("Manufacturer Name"), aliases),
                        "image_url": N.clean(row.get("Image URL")), "price": price})
    return out, dict(skipped)


def _summarize(rows: list[dict], matched_by: str, score: float) -> tuple:
    price = round(statistics.median(r["price"] for r in rows), 2)
    detail = f"row {rows[0]['row']}: {rows[0]['name'][:200]}"
    if len(rows) > 1:
        detail += f" (+{len(rows) - 1} more rows, median)"
    return price, matched_by, score, detail


def match_sheet(cur, priced: list[dict], threshold: float = DEFAULT_THRESHOLD) -> dict[int, tuple]:
    """{supply_id: (price, matched_by, score, detail)} for every supply a priced row matches."""
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

    # 3. trigram similarity, conservatively
    rest = [s for s in supplies if s[0] not in hits and len(s[1]) >= 3]
    if rest and priced:
        cur.execute("DROP TABLE IF EXISTS _price_sp, _price_pr")
        cur.execute("CREATE TEMP TABLE _price_sp (supply_id BIGINT, name TEXT, mfr TEXT) ON COMMIT DROP")
        cur.execute("CREATE TEMP TABLE _price_pr (i INT, name TEXT, mfr TEXT) ON COMMIT DROP")
        with cur.copy("COPY _price_sp FROM STDIN") as cp:
            for row in rest:
                cp.write_row(row)
        with cur.copy("COPY _price_pr FROM STDIN") as cp:
            for i, p in enumerate(priced):
                cp.write_row((i, p["key"], p["mfr"]))
        cur.execute("SELECT s.supply_id, s.name, p.i, p.name, similarity(s.name, p.name) AS sim "
                    "FROM _price_sp s JOIN _price_pr p ON similarity(s.name, p.name) >= %s "
                    "AND (s.mfr = '' OR p.mfr = '' OR s.mfr = p.mfr) ORDER BY s.supply_id, sim DESC",
                    (threshold,))
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

    out = {}
    for sid, (rows, how, score) in hits.items():
        match = _summarize(rows, how, score)
        if match[0] > 0:
            out[sid] = match
    return out


# ---------------------------------------------------------------------------
# writing prices
# ---------------------------------------------------------------------------
_SET_PRICE = """
UPDATE supply s SET unit_value_usd = %(price)s
 WHERE s.supply_id = %(sid)s
   AND (%(force)s OR s.unit_value_usd IS NULL OR EXISTS (
        SELECT 1 FROM (SELECT p.source, p.price FROM supply_price p WHERE p.supply_id = s.supply_id
                        ORDER BY p.fetched_at DESC LIMIT 1) last
         WHERE last.source <> 'manual' AND last.price = s.unit_value_usd))
RETURNING supply_id"""

_RECORD = """
INSERT INTO supply_price (supply_id, source, price, matched_by, score, detail, fetched_at)
VALUES (%(sid)s, %(source)s, %(price)s, %(matched_by)s, %(score)s, %(detail)s, clock_timestamp())
ON CONFLICT (supply_id, source) DO UPDATE
   SET price = EXCLUDED.price, matched_by = EXCLUDED.matched_by, score = EXCLUDED.score,
       detail = EXCLUDED.detail, fetched_at = EXCLUDED.fetched_at"""


def apply_prices(cur, matches: dict[int, tuple], source: str) -> dict:
    """Write {supply_id: (price, matched_by, score, detail)}. Automatic sources
    never overwrite a manually set price; source 'manual' always wins."""
    stats = {"set": 0, "kept_manual": 0}
    for sid, (price, matched_by, score, detail) in matches.items():
        args = {"sid": sid, "price": price, "force": source == MANUAL, "source": source,
                "matched_by": matched_by, "score": score, "detail": detail}
        cur.execute(_SET_PRICE, args)
        if cur.fetchone() is None:
            stats["kept_manual"] += 1
            continue
        cur.execute(_RECORD, args)
        stats["set"] += 1
    return stats


# ---------------------------------------------------------------------------
# B4P Pricing Enrichment API (f25_b4p_tech_project)
# ---------------------------------------------------------------------------
def enrich_via_api(cur, url: str, api_key: str, limit: int = 100, batch_size: int = 25,
                   max_price: float = DEFAULT_MAX_PRICE, client: httpx.Client | None = None) -> tuple[dict, dict]:
    """Send up to `limit` unpriced supplies (most units first) to
    POST <url>/v1/enrich/batch. Returns ({supply_id: match}, stats).
    Keeps an answer only if status == 'priced', needs_review is false, the
    currency is USD and 0 < unit_price <= max_price. Stops at the first HTTP error."""
    cur.execute("SELECT supply_id, name, COALESCE(manufacturer, ''), category, COALESCE(general_name, ''), "
                "units, COALESCE(image_url, '') FROM v_unpriced ORDER BY units DESC, supply_id LIMIT %s",
                (limit,))
    items = [{"external_id": str(sid), "name": name, "manufacturer_name": mfr, "category": cat,
              "subcategories": cat, "general": gen, "quantity": int(units), "image_url": img}
             for sid, name, mfr, cat, gen, units, img in cur.fetchall()]
    stats = {"sent": 0, "priced": 0, "rejected": 0, "cost_usd": 0.0, "error": None}
    matches = {}
    own_client = client is None
    client = client or httpx.Client(timeout=300)
    endpoint = url.rstrip("/") + "/v1/enrich/batch"
    try:
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            try:
                r = client.post(endpoint, json={"items": batch},
                                headers={"Authorization": f"Bearer {api_key}"})
                r.raise_for_status()
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                first_line = (str(e).splitlines() or [""])[0]
                stats["error"] = f"{endpoint}: {type(e).__name__} {first_line[:150]}"
                log.error("pricing API: %s", stats["error"])
                break
            stats["sent"] += len(batch)
            stats["cost_usd"] += float((body.get("usage") or {}).get("cost_usd") or 0)
            sent_ids = {int(i["external_id"]) for i in batch}
            for res in body.get("items") or []:
                sid = int(res["external_id"]) if str(res.get("external_id") or "").isdigit() else None
                price = res.get("unit_price")
                ok = (sid in sent_ids and res.get("status") == "priced" and not res.get("needs_review")
                      and (res.get("currency") or "USD") == "USD"
                      and isinstance(price, (int, float)) and 0 < price <= max_price
                      and round(price, 2) > 0)
                if not ok:
                    stats["rejected"] += 1
                    continue
                detail = " ".join(x for x in (res.get("matched_item"), res.get("price_url")) if x)
                matches[sid] = (round(float(price), 2), f"f25:{res.get('source') or 'unknown'}",
                                res.get("confidence"), detail[:500] or None)
                stats["priced"] += 1
    finally:
        if own_client:
            client.close()
    return matches, stats


# ---------------------------------------------------------------------------
# CSV for humans
# ---------------------------------------------------------------------------
EXPORT_COLUMNS = ["supply_id", "name", "manufacturer", "category", "general_name",
                  "active_rows", "units", "image_url", "unit_value_usd"]


def export_unpriced(cur, f) -> int:
    """Write v_unpriced as CSV (most units first) with an empty unit_value_usd column."""
    cur.execute("SELECT supply_id, name, manufacturer, category, general_name, active_rows, units, "
                "image_url FROM v_unpriced ORDER BY units DESC, supply_id")
    w = csv.writer(f)
    w.writerow(EXPORT_COLUMNS)
    n = 0
    for row in cur.fetchall():
        w.writerow([("" if v is None else v) for v in row] + [""])
        n += 1
    return n


def read_manual_csv(f) -> tuple[dict[int, tuple], list[str]]:
    """Filled-in export -> ({supply_id: match}, problems). Blank prices are skipped."""
    matches, problems = {}, []
    for line, row in enumerate(csv.DictReader(f), start=2):
        cell = (row.get("unit_value_usd") or "").strip()
        if not cell:
            continue
        sid, price = (row.get("supply_id") or "").strip(), N.parse_money(cell)
        if not sid.isdigit() or price is None or N.is_negative(cell):
            problems.append(f"line {line}: bad supply_id {sid!r} or price {cell!r}")
            continue
        matches[int(sid)] = (round(price, 2), "csv", None, None)
    return matches, problems


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
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
