#!/usr/bin/env python3
"""Regenerate b4p-service/tests/fixtures/normalize_golden.json.

Collects every distinct value the live feed has ever had in the columns the
normalizers parse (from data/supplies.json + data/requests.json, plus any
values already in the golden file), runs them through load.py's reference
implementation, and writes input -> expected output. The C++ unit test then
checks normalize.cpp against every one of them.

Only parsed columns are included — no names, notes, emails or image URLs —
so the fixture is safe to commit.

    python3 scripts/gen_golden.py            # after downloading fresh data/
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import load  # noqa: E402

GOLDEN = ROOT / "b4p-service/tests/fixtures/normalize_golden.json"

COLUMNS = {  # function -> (feed file, column)
    "norm_category": ("supplies.json", "Category"),
    "norm_mfr":      ("supplies.json", "Manufacturer Name"),
    "parse_qty":     ("supplies.json", "Quantity"),
    "parse_date":    ("supplies.json", "Date of Expiration"),
    "parse_pallet":  ("supplies.json", "Pallet Number"),
    "parse_box":     ("supplies.json", "Box Number"),
    "map_status":    ("requests.json", "Status"),
}


def expected(fn, value):
    if fn == "norm_category":
        return load.norm_category(value)
    if fn == "norm_mfr":
        return load.norm_mfr(value)
    if fn == "parse_qty":
        return load.parse_qty(value)
    if fn == "parse_date":
        d = load.parse_date(value)
        return d.isoformat() if d else None
    if fn == "parse_pallet":
        return load.parse_pallet(value)
    if fn == "parse_box":
        b = load.parse_box(value)
        return list(b) if b else None
    if fn == "map_status":
        return load.STATUS_MAP.get((value or "").strip().lower(), "requested")
    raise KeyError(fn)


def compute(inputs):
    return {fn: {v: expected(fn, v) for v in sorted(vals)} for fn, vals in inputs.items()}


def main():
    inputs = {fn: set() for fn in COLUMNS}
    if GOLDEN.exists():
        for fn, table in json.loads(GOLDEN.read_text()).items():
            inputs.setdefault(fn, set()).update(table)
    for fn, (fname, col) in COLUMNS.items():
        path = ROOT / "data" / fname
        if path.exists():
            for row in json.loads(path.read_text()):
                v = row.get(col)
                if isinstance(v, str):
                    inputs[fn].add(v)
    golden = compute(inputs)
    GOLDEN.write_text(json.dumps(golden, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN.relative_to(ROOT)}: "
          + ", ".join(f"{fn} {len(t)}" for fn, t in golden.items()))


if __name__ == "__main__":
    main()
