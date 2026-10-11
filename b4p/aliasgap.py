"""Alias-gap report: spellings in the sheet that the alias tables don't map yet,
with ready-to-run INSERT statements (review them before running).

Read-only. Names are free text from the sheet, so they go through redact_phi
before they are printed.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

from b4p import normalize as N


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _close(word: str, choices, cutoff: float = 0.8) -> str | None:
    hit = difflib.get_close_matches(word, list(choices), n=1, cutoff=cutoff)
    return hit[0] if hit else None


def alias_gaps(conn) -> dict[str, list[tuple[str, int, str | None]]]:
    """{'category': [(spelling, rows, suggested canonical or None)], 'manufacturer': ..., 'box_type': ...}"""
    from b4p.sync import load_rules  # local import: sync imports normalize, not us
    with conn.cursor() as cur:
        rules = load_rules(cur)
        cur.execute("SELECT redact_phi(COALESCE(raw ->> 'Subcategory', raw ->> 'Subcategories', raw ->> 'Category')), "
                    "count(*) FROM inventory_item WHERE removed_at IS NULL GROUP BY 1")
        cat_cells = cur.fetchall()
        cur.execute("SELECT redact_phi(m.name::TEXT), count(*) FROM inventory_item i JOIN manufacturer m USING (manufacturer_id) "
                    "WHERE i.removed_at IS NULL GROUP BY 1")
        mfrs = cur.fetchall()
        cur.execute("SELECT redact_phi(b.box_type::TEXT), count(*) FROM inventory_item i JOIN box b USING (box_id) "
                    "WHERE i.removed_at IS NULL GROUP BY 1")
        boxes = cur.fetchall()

    out: dict[str, list] = {"category": [], "manufacturer": [], "box_type": []}

    approved = {c.lower(): c for c in N.APPROVED_CATEGORIES}
    for cell, n in cat_cells:
        cell = N.clean(cell)
        if not cell:
            continue
        got = N.norm_category(cell, rules["category"])
        if got == N.FALLBACK_CATEGORY and cell.lower() not in approved and cell.lower() not in rules["category"]:
            guess = _close(cell.lower(), approved)
            out["category"].append((cell, n, approved.get(guess) if guess else None))

    names = Counter({m: n for m, n in mfrs})
    sq = {m: _squash(m) for m in names}
    parent = {m: m for m in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for m in names:  # same name up to case/punctuation/spacing, or one spelling is a prefix of the other
        for o in names:
            if m < o and sq[m] and sq[o] and (sq[m] == sq[o] or (len(sq[m]) >= 4 and sq[o].startswith(sq[m]))
                                              or (len(sq[o]) >= 4 and sq[m].startswith(sq[o]))):
                parent[find(m)] = find(o)
    clusters: dict[str, list[str]] = {}
    for m in names:
        clusters.setdefault(find(m), []).append(m)
    for group in clusters.values():
        if len(group) > 1:
            best = max(group, key=lambda g: (names[g], -len(g), g))
            out["manufacturer"] += [(g, names[g], best) for g in group if g != best]

    known = set(N.KNOWN_BOX_TYPES)
    for b, n in boxes:
        b = N.clean(b)
        if b and b.upper() not in known and b.upper() not in {k.upper() for k in rules["box_type"]}:
            guess = _close(b.upper(), known, 0.6)
            if guess:  # only likely typos / short forms; long labels are just other box names
                out["box_type"].append((b, n, guess))
    for v in out.values():
        v.sort(key=lambda t: -t[1])
    return out


def needs_a_human(spelling: str) -> bool:
    """'A / B' and 'A (B)' spellings may name two companies; never auto-suggest them."""
    return " / " in spelling or "(" in spelling


def suggestions(gaps: dict) -> list[str]:
    """Lines of SQL for the gaps that have a suggested canonical name (co-brand spellings excluded)."""
    sql = []
    for spelling, _, canon in gaps["category"]:
        if canon:
            sql.append(f"INSERT INTO category_alias (alias, category_id) SELECT {_q(spelling.lower())}, category_id "
                       f"FROM category WHERE name = {_q(canon)} ON CONFLICT DO NOTHING;")
    for spelling, _, canon in gaps["manufacturer"]:
        if canon and not needs_a_human(spelling):
            sql.append(f"INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT {_q(spelling.lower())}, "
                       f"manufacturer_id FROM manufacturer WHERE name = {_q(canon)} ON CONFLICT DO NOTHING;")
    for spelling, _, canon in gaps["box_type"]:
        if canon:
            sql.append(f"INSERT INTO box_type_alias (alias, code) VALUES ({_q(spelling.upper())}, {_q(canon)}) "
                       f"ON CONFLICT DO NOTHING;")
    return sql
