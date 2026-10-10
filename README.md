# Blueprints_DB
Database for Blueprints for Pangaea (B4P).

B4P collects surplus medical supplies and ships them to clinics that need them.
Volunteers photograph each item with the inventorying app, which writes a row to
a Google Sheet. This repo turns that sheet (plus the external dashboard's
organization requests) into a proper PostgreSQL database that both dashboards
read from.

History: in September 2026 I designed a BCNF schema, loaded the Google Sheet
and dashboard data into Postgres with `load.py`, and kept it current with a
background `monitor.py` (my newfound knowledge after a couple lessons on
SQLbolt :). After a detour through a C++ service, it is now one small Python
package that any volunteer can run and change. The old code lives in
[`legacy/`](legacy/README.md) for reference.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
createdb b4p_v2 && cp .env.example .env      # DATABASE_URL=postgresql:///b4p_v2
python -m b4p init && python -m b4p sync      # create tables, pull live data
python -m b4p status && pytest                # counts; run the tests
```

## How it fits together

```
 Inventory app ──writes──▶ Google Sheet
 (photo + AI label)              │  python -m b4p sync (read-only service account)
                                 │  GitHub Actions, every 15 min
                                 ▼
                              Postgres (Supabase)
                               tables + dash_* views/functions
                                  │                 │
                                  ▼                 ▼
                       internal dashboard   external dashboard
                       (KPIs, quantities)   (clinic requests)
```

- **Sync** (`b4p/sync.py`): each sheet row becomes one `inventory_item`,
  matched by its Image URL. New rows are added, edited rows updated in place,
  rows deleted from the sheet are marked `removed_at` (never deleted). Running
  it again with no changes does nothing.
- **Requests** live only in the database: the external dashboard creates them
  with `create_request` and updates them with `set_request_status`.
- **Safety rails**: the sync stops without changing anything if a required
  column (Name, Quantity, Image URL, Manufacturer Name, Date of Expiration,
  Pallet Number, Box Number, Subcategory/Category) is missing or renamed; it
  changes nothing if fewer than half of the sheet's rows are items it already
  knows (wrong tab? set `B4P_MIN_KEY_OVERLAP=0` for one run if the sheet really
  was replaced); it won't let the number of active items drop more than 15%
  below the most it had in the last 24 hours (set `B4P_MAX_REMOVE_FRACTION=1`
  for one run if a big deletion is real). Both checks start once there are 20+
  items, so a first load works. A row with an unusable cell is cleaned or
  skipped with a warning (unreadable expiration dates flag the row for review);
  every run, even a failed one, leaves a row in `sync_run` (see
  `python -m b4p status`). Copy-pasted rows that share a photo URL keep their
  own ids, even when edited.
- **Dashboards** only use the views `dash_items`, `dash_supplies`,
  `dash_requests` and the functions `create_request`, `set_request_status`,
  `set_item_quantity`. See [docs/CONTRACT.md](docs/CONTRACT.md).

## Commands

| command | what it does |
|---|---|
| `python -m b4p init` | create/update all tables, views, functions (safe to re-run) |
| `python -m b4p sync` | pull the inventory sheet once |
| `python -m b4p sync --loop 300` | keep syncing every 5 min (replaces `monitor.py`) |
| `python -m b4p check` | consistency checks (expired stock, rows awaiting review, ...) |
| `python -m b4p status` | counts and the last sync |

`DATABASE_URL` is read from the environment, falling back to `.env`
(an environment variable always wins).

## Code map

```
sql/schema.sql      the whole database: tables, views, dashboard contract
b4p/normalize.py    messy cell text -> clean values (categories, dates, boxes...)
b4p/sources.py      read the sheet (service account or public CSV)
b4p/sync.py         upsert into Postgres, record a sync_run row
b4p/checks.py       consistency checks
b4p/cli.py          the commands above
tests/              pytest suite on real data snapshots in tests/fixtures/
```

Main tables: `inventory_item` (one per sheet row), `supply` (distinct products),
`category`, `manufacturer`, `box`, `box_type` (each with an `*_alias` table:
add a row there to teach the sync a new spelling), `requester`, `shipment`,
`shipment_item`, `sync_run`. Handy views: `v_inventory`, `v_kpis`,
`v_expiring`, `v_review_queue`.

## Hosting on Supabase

1. Create a project at [supabase.com](https://supabase.com) (free tier is plenty).
2. Project Settings → Database → Connection string → **Session pooler** URI.
   Put it in `.env` as `DATABASE_URL=...` (and replace `[YOUR-PASSWORD]`).
3. `python -m b4p init && python -m b4p sync`
4. In GitHub: Settings → Secrets and variables → Actions → New secret
   `DATABASE_URL` with the same URL. The `sync` workflow then runs every 15
   minutes (Actions tab → sync → "Run workflow" to trigger it by hand).
   Also add `GOOGLE_SERVICE_ACCOUNT_JSON` (the service-account key file's
   contents; the account needs Viewer access to the sheet). Without it the sync
   can only use the public CSV export, which currently requires a login, so every
   run would fail (and say so in `sync_run`).
5. Give the dashboards the same connection string (or a restricted role).

## Notes on the data

- **Where inventory is read from**: the sheet via a Google service account
  (`GOOGLE_SERVICE_ACCOUNT_JSON` = key JSON, or `GOOGLE_APPLICATION_CREDENTIALS`
  = key path; read-only scope), else the sheet's public CSV export. There is
  deliberately no fallback to the dashboards' APIs (they serve this database, so
  the sync would read its own output back). `sync_run.source` records which was
  used. Tab: `INVENTORY_WORKSHEET_GID` (default `1019735220`, "Final
  Inventorying"; the internal dashboard used to read `384285232`, "[AVAILABLE]
  Final Inventorying", a different set of rows).
- **Requests**: the database is the source of truth. A one-time import of old
  requests is possible but off by default: set `REQUESTS_URL` (a JSON feed) or
  `ORG_REQUESTS_WORKSHEET_TITLE` (a sheet tab, read with the service account).
  Imports only add request ids the database doesn't have.
- The current sheet uses `Category` for the box label (`ST/N`, `TC/RC`, ...)
  and `Subcategory` for the approved category; the older layout (category in
  `Category`, `ER #1` in `Box Number`) is still understood.
- There is no "date inventoried" column yet, so `warehouse_date` is the day the
  sync first saw the row.

## Tests

```bash
pytest              # creates and drops a throwaway database b4p_test
pytest -m live      # also syncs from the real feeds
```

Set `B4P_TEST_DATABASE_URL` to test against another server (CI does this).
