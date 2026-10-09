# legacy/

Kept for reference only. **Nothing in here is used anymore**; the `b4p/`
Python package at the repo root replaces all of it.

- `cpp-service/` — the C++ (Drogon) sync/API service. Retired: too hard for a
  volunteer team to build and maintain.
- `python-scripts/` — the original `load.py` / `monitor.py` (and the golden-file
  generator). Replaced by `python -m b4p sync` / `python -m b4p sync --loop`.
- `sql/` — the original `schema.sql` and `001_sync_ledger_review.sql`
  migration. Merged and simplified into `sql/schema.sql`.
