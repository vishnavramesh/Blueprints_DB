"""Public impact numbers (view public_impact) exported as a static JSON file."""
from __future__ import annotations

import json
from pathlib import Path


def fetch(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT units_on_hand, distinct_products, categories, units_shipped, "
                    "requests_fulfilled, partner_orgs, last_updated FROM public_impact")
        u, p, cats, shipped, fulfilled, orgs, ts = cur.fetchone()
    return {
        "unitsOnHand": u,
        "distinctProducts": p,
        "categories": [{"name": c["category"], "units": c["units"], "items": c["items"]} for c in cats],
        "unitsShipped": shipped,
        "requestsFulfilled": fulfilled,
        "partnerOrgs": orgs,
        "lastUpdated": ts.isoformat(timespec="seconds") if ts else None,
    }


def write_json(conn, out: Path) -> dict:
    data = fetch(conn)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data
