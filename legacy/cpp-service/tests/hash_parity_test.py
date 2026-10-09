#!/usr/bin/env python3
"""C++ row_hash == Python md5(json.dumps(row, sort_keys=True)), byte for byte.

Every source_hash in the database was written by the Python loader; if the C++
hash drifts on any character, sync would see every such row as "edited".
Checks synthetic edge cases (always) plus every real row in data/ (if present).
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

repo = Path(os.environ.get("B4P_REPO", Path(__file__).resolve().parents[2]))
tool = os.environ.get("B4P_ROWHASH_BIN", str(repo / "b4p-service/build/b4p-rowhash"))

EDGE = [
    {"Name": "plain"},
    {"Name": 'quote " backslash \\ slash /'},
    {"Name": "tab\tnewline\ncr\rbs\bff\f"},
    {"Name": "control \x01\x1f and DEL \x7f"},
    {"Name": "Misc Non‐Medical ‑ non-breaking"},
    {"Name": "LNCS® Neo-3 ™ café µm"},
    {"Name": "emoji \U0001F489 syringe \U0001F9EA"},
    {"Name": "CJK 注射器"},
    {"Name": "", "Quantity": "", "Notes": None},
    {"b": "2", "a": "1", "A": "0", "_sheet_row": "7", "Unnamed_13": ""},
    {"Nested": {"z": [1, "two", None, True]}, "Num": 3},
]


def py_hash(row):
    return hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()


rows = list(EDGE)
supplies = repo / "data" / "supplies.json"
if supplies.exists():
    rows += json.loads(supplies.read_text())

out = subprocess.run([tool], input=json.dumps(rows).encode(), capture_output=True, check=True)
cpp = out.stdout.decode().split()
assert len(cpp) == len(rows), f"tool returned {len(cpp)} hashes for {len(rows)} rows"

bad = 0
for row, got in zip(rows, cpp):
    want = py_hash(row)
    if got != want:
        bad += 1
        if bad <= 10:
            print(f"FAIL {json.dumps(row, sort_keys=True)[:100]}\n     python {want}\n     c++    {got}")
if bad:
    print(f"{bad}/{len(rows)} hashes differ")
    sys.exit(1)
print(f"hash parity ok: {len(rows)} rows ({len(EDGE)} edge cases)")
