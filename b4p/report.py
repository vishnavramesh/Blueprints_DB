"""Operations report for HQ volunteers: one static HTML page.

Read-only and meant to be generated locally (`python -m b4p report`), not
published: item names can carry patient identifiers from specimen-tube photos.
Text comes from the redacted v_inventory view (the database's redact_phi, the
same rule every dashboard uses), rows with personal data in any cell are left
out entirely, and requester names/emails/notes are never selected.
"""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import date
from pathlib import Path

LOW_STOCK_DEFAULT = 5
LIST_LIMIT = 50  # rows shown per table; the true count is always printed
WARN_LIMIT = 25

_ALL_ROWS = r"""
SELECT v.sheet_row, v.name, (b4p_raw_has_phi(i.raw) OR b4p_has_phi(i.raw_name)
        OR v.name ~* '\m(pt|patient)\.?\s*(id|no|number|num|#)') AS has_phi, v.category, v.quantity, v.expiration_date,
       v.pallet_number, v.box_type, v.box_number, v.review,
       (i.flagged AND i.reviewed_at IS NULL) AS awaiting_review,
       redact_phi(COALESCE(i.raw ->> 'Date of Expiration', '')) AS raw_expiration
FROM v_inventory v JOIN inventory_item i USING (item_id)
ORDER BY v.sheet_row"""
_COLS = ["sheet_row", "name", "has_phi", "category", "quantity", "expiration_date", "pallet",
         "box_type", "box_number", "review", "awaiting_review", "raw_expiration"]


def _reasons(r: dict) -> str:
    name = r["name"] or ""
    out = []
    if not name or name.startswith("{") or name.lower() in ("unknown", "n/a"):
        out.append("name unreadable")
    if (r["review"] or "").lower() not in ("", "no", "no review needed", "false"):
        out.append(f"Review = {r['review']}")
    if r["expiration_date"] is None and r["raw_expiration"]:
        out.append("expiration date unreadable")
    return "; ".join(out)


def collect(conn, low_stock: int = LOW_STOCK_DEFAULT) -> dict:
    """Gather everything the report shows as plain lists/numbers."""
    with conn.cursor() as cur:
        cur.execute("SELECT CURRENT_DATE")
        today = cur.fetchone()[0]
        cur.execute(_ALL_ROWS)
        everything = [dict(zip(_COLS, r)) for r in cur.fetchall()]
        # specimen tubes: names carry patient IDs, so these rows never appear
        rows = [r for r in everything if not r["has_phi"]]
        data: dict = {"today": today, "low_stock_threshold": low_stock, "withheld": len(everything) - len(rows)}

        def expiring(lo: int | None, hi: int | None) -> list[dict]:
            out = []
            for r in rows:
                e = r["expiration_date"]
                if e is None or r["quantity"] <= 0:
                    continue
                days = (e - today).days
                if (lo is None or days >= lo) and (hi is None or days <= hi):
                    out.append(r)
            return sorted(out, key=lambda r: (r["expiration_date"], r["sheet_row"]))

        data["expired"] = expiring(None, -1)
        data["exp30"] = expiring(0, 30)
        data["exp90"] = expiring(31, 90)

        totals: dict = defaultdict(lambda: [0, 0])
        for r in rows:
            if r["name"] and not r["name"].startswith("{"):
                t = totals[(r["name"], r["category"])]
                t[0] += r["quantity"]
                t[1] += 1
        data["low"] = sorted(((n, c, u, k) for (n, c), (u, k) in totals.items() if u <= low_stock),
                             key=lambda x: (x[2], x[0]))
        data["review"] = [r | {"reasons": _reasons(r)} for r in rows if r["awaiting_review"]]

        cur.execute("SELECT total_entries, total_raw_items, box_count, pallet_count, awaiting_review FROM v_kpis")
        data["kpis"] = dict(zip(["entries", "units", "boxes", "pallets", "awaiting_review"], cur.fetchone()))
        cur.execute("""
            SELECT pallet_number, count(DISTINCT (box_type, box_number)) AS boxes, count(*) AS rows,
                   sum(quantity)::INT AS units
            FROM v_inventory WHERE pallet_number IS NOT NULL GROUP BY pallet_number ORDER BY pallet_number""")
        data["boxes"] = cur.fetchall()
        cur.execute("SELECT count(*), COALESCE(sum(quantity), 0) FROM v_inventory WHERE pallet_number IS NULL")
        data["unassigned"] = cur.fetchone()
        cur.execute("""
            SELECT sh.status, count(*) AS requests,
                   COALESCE(sum((SELECT sum(quantity_requested) FROM shipment_item si
                                 WHERE si.shipment_id = sh.shipment_id)), 0)::INT AS units
            FROM shipment sh GROUP BY sh.status ORDER BY requests DESC, sh.status""")
        data["requests"] = cur.fetchall()
        cur.execute("""SELECT run_id, finished_at, source, items_added, items_updated, items_removed,
                              rows_skipped, redact_phi(error),
                              ARRAY(SELECT redact_phi(w) FROM jsonb_array_elements_text(warnings) w)
                         FROM sync_run ORDER BY run_id DESC LIMIT 1""")
        last = cur.fetchone()
        data["sync"] = None if last is None else dict(zip(
            ["run_id", "finished_at", "source", "added", "updated", "removed", "skipped", "error", "warnings"],
            last))
    return data


# ---------------------------------------------------------------------------
# rendering: one small table model feeds both Markdown and HTML
# ---------------------------------------------------------------------------
ITEM_HEAD = ["Row", "Item", "Category", "Qty", "Expires", "Pallet", "Box"]


def _item_table(rows):
    return [[r["sheet_row"], r["name"], r["category"], r["quantity"], r["expiration_date"], r["pallet"],
             f"{r['box_type'] or ''} {r['box_number'] if r['box_number'] is not None else ''}".strip()]
            for r in rows]


def _truncated(n: int, limit: int) -> str:
    return f"Showing first {limit} of {n}." if n > limit else ""


def sections(d: dict) -> list[dict]:
    """[{title, note, head, rows}] in display order."""
    k, s = d["kpis"], d["sync"]
    out = [{"title": "Summary", "note": "", "head": ["Measure", "Value"], "rows": [
        ["Inventory rows", k["entries"]], ["Units on hand", k["units"]], ["Boxes", k["boxes"]],
        ["Pallets", k["pallets"]], ["Rows awaiting review", k["awaiting_review"]],
        ["Specimen items withheld", d["withheld"]]]}]
    for key, title in (("expired", "Already expired, still in stock"),
                       ("exp30", "Expiring within 30 days"),
                       ("exp90", "Expiring in 31 to 90 days")):
        rows = d[key]
        out.append({"title": f"{title} ({len(rows)})", "note": _truncated(len(rows), LIST_LIMIT),
                    "head": ITEM_HEAD, "rows": _item_table(rows[:LIST_LIMIT])})
    low = d["low"]
    out.append({"title": f"Low stock: {d['low_stock_threshold']} units or fewer per product ({len(low)})",
                "note": _truncated(len(low), LIST_LIMIT),
                "head": ["Item", "Category", "Units", "Rows"], "rows": [list(r) for r in low[:LIST_LIMIT]]})
    rev = d["review"]
    out.append({"title": f"Review queue ({len(rev)})", "note": _truncated(len(rev), LIST_LIMIT),
                "head": ["Row", "Item", "Category", "Qty", "Why flagged"],
                "rows": [[r["sheet_row"], r["name"], r["category"], r["quantity"], r["reasons"]]
                         for r in rev[:LIST_LIMIT]]})
    un_rows, un_units = d["unassigned"]
    out.append({"title": "Boxes and pallets",
                "note": f"Not on a pallet: {un_rows} rows, {un_units} units.",
                "head": ["Pallet", "Boxes", "Rows", "Units"], "rows": [list(r) for r in d["boxes"]]})
    out.append({"title": "Requests by status", "note": "Counts only; no organizations or contacts.",
                "head": ["Status", "Requests", "Units requested"], "rows": [list(r) for r in d["requests"]]})
    if s is None:
        out.append({"title": "Last sync", "note": "No sync has run yet.", "head": [], "rows": []})
    else:
        when = f"{s['finished_at']:%Y-%m-%d %H:%M}" if s["finished_at"] else "unfinished"
        status = "FAILED: " + s["error"] if s["error"] else "ok"
        warns = s["warnings"] or []
        out.append({"title": "Last sync", "note": "", "head": ["Measure", "Value"], "rows": [
            ["Run", f"#{s['run_id']} at {when} ({s['source'] or 'unknown source'})"], ["Status", status],
            ["Rows added / updated / removed / skipped",
             f"{s['added']} / {s['updated']} / {s['removed']} / {s['skipped']}"],
            ["Warnings", len(warns)]]})
        if warns:
            out.append({"title": "Sync warnings", "note": _truncated(len(warns), WARN_LIMIT),
                        "head": ["Warning"], "rows": [[w] for w in warns[:WARN_LIMIT]]})
    return out


_CSS = ("body{font:15px/1.45 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1d2433}"
        "h1{font-size:1.5rem}h2{font-size:1.1rem;margin-top:2rem;border-bottom:1px solid #d7dce5}"
        "table{border-collapse:collapse;width:100%;font-size:.9rem}"
        "th,td{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #eceff4;vertical-align:top}"
        "th{background:#f4f6fa}.note{color:#5b6475;font-size:.85rem}")


def render_html(d: dict) -> str:
    def e(v) -> str:
        return html.escape("" if v is None else str(v))

    parts = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
             "<meta name=viewport content='width=device-width,initial-scale=1'>",
             f"<title>B4P operations report {d['today']}</title><style>{_CSS}</style></head><body>",
             f"<h1>B4P operations report, {d['today']}</h1>",
             f"<p class=note>{d['withheld']} specimen items withheld (patient identifiers). "
             "Internal use only; do not publish.</p>"]
    for sec in sections(d):
        parts.append(f"<h2>{e(sec['title'])}</h2>")
        if sec["note"]:
            parts.append(f"<p class=note>{e(sec['note'])}</p>")
        if sec["head"] and sec["rows"]:
            parts.append("<table><thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in sec["head"])
                         + "</tr></thead><tbody>")
            parts += ["<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in sec["rows"]]
            parts.append("</tbody></table>")
        elif sec["head"]:
            parts.append("<p class=note>None.</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def default_path(today: date | None = None) -> Path:
    return Path("reports") / f"ops-{today or date.today():%Y-%m-%d}.html"


def write_report(conn, out: Path | None = None, low_stock: int = LOW_STOCK_DEFAULT) -> Path:
    """Writes the HTML report and returns its path."""
    d = collect(conn, low_stock)
    out = Path(out) if out else default_path(d["today"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(d), encoding="utf-8")
    return out
