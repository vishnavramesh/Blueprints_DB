#!/usr/bin/env python3
"""The golden fixture must still agree with load.py (the reference rules).

normalize_test.cpp checks C++ == golden; this checks golden == Python. Together
they pin C++ == Python on every real feed value without the C++ test needing
a Python interpreter.
"""
import json
import os
import sys
from pathlib import Path

repo = Path(os.environ.get("B4P_REPO", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(repo / "scripts"))
import gen_golden  # noqa: E402

committed = json.loads(gen_golden.GOLDEN.read_text())
fresh = gen_golden.compute({fn: set(t) for fn, t in committed.items()})
bad = [(fn, v, committed[fn][v], fresh[fn][v])
       for fn in committed for v in committed[fn] if committed[fn][v] != fresh[fn][v]]
for fn, v, old, new in bad:
    print(f"FAIL {fn}({v!r}): golden says {old!r}, load.py now says {new!r}")
if bad:
    print("rules changed: rerun scripts/gen_golden.py and review the diff")
    sys.exit(1)
print(f"golden fixture agrees with load.py ({sum(len(t) for t in committed.values())} values)")
