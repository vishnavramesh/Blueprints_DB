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

## Quickstart (for volunteers)

You need Python 3 and PostgreSQL on your computer. Open a terminal in this
folder, then:

```bash
python3 -m venv .venv && source .venv/bin/activate     # once: a private Python for this project
pip install -e '.[test]'                                # once: install it
createdb b4p_v2 && cp .env.example .env                 # once: an empty practice database
python -m b4p doctor                                    # 1. always start here
python -m b4p init                                      # create the tables (safe to repeat)
python -m b4p sync                                      # pull the latest sheet
python -m b4p status                                    # see what you have
python -m b4p check --details                           # see what needs attention, row by row
```

**`doctor` first.** It looks at your setup and tells you, in plain words,
which database you are pointed at (it never prints a password), whether it can
connect, whether the tables and dashboard views exist, and whether the sheet
credentials are present (it never shows them). It changes nothing. If it says
FAIL, fix that line before anything else. If the database line says REMOTE,
you are pointed at the live database, not your practice one: stop and ask.

What the other commands tell you:

| you see | it means | what to do |
|---|---|---|
| `status` says `last sync: FAILED` | the sheet could not be read, or a safety guard stopped the sync | read the line under it; the data on the dashboards is the last good copy |
| `check` shows WARN lines | things a person should look at (expired stock, rows to review, ...) | run `check --details` to see the exact sheet rows and boxes |
| `check --aliases` | the sheet spells a company or box name two different ways | it prints suggestions; never run them without a second pair of eyes |

Tests (`pytest`) are for people changing the code, not for everyday use.

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
- **Privacy**: some items are specimen tubes labelled with patient IDs. The
  tables keep the original text for HQ, but every view (everything a dashboard
  reads) shows **every text column** (name, notes, General, lot number,
  manufacturer, category, review, box label, ...) through `redact_phi()`, and
  hides the photo link of any row where any of those cells had something to
  redact; the sync flags such rows for review ("contains patient identifier").
  Give dashboards access to the views only.
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
  `dash_requests`, `dash_sync_status` and the functions `create_request`, `set_request_status`,
  `set_item_quantity`. See [docs/CONTRACT.md](docs/CONTRACT.md).

## Commands

| command | what it does |
|---|---|
| `python -m b4p init` | create/upgrade tables; drop and recreate all views/functions (safe to re-run, any version) |
| `python -m b4p sync` | pull the inventory sheet once |
| `python -m b4p sync --loop 300` | keep syncing every 5 min (replaces `monitor.py`) |
| `python -m b4p check [--details] [--limit N]` | consistency checks (expired stock, rows awaiting review, ...); `--details` lists the rows (sheet row, box, pallet) behind each warning |
| `python -m b4p check --aliases` | spellings no alias maps yet, with suggested `INSERT` rows (a guess: review them) |
| `python -m b4p doctor` | read-only setup check: which database it points at (no password), views/functions exist, credentials present (never shown) |
| `python -m b4p status` | counts and the last sync |
| `python -m b4p report [--out F.html]` | static HTML ops report (expiring, low stock, review queue, requests by status, last sync). Internal only: generate it locally, never publish or upload it (this repo is public; item names can contain patient IDs, which the report masks or withholds) |
| `python -m b4p impact [--out impact.json]` | aggregate-only numbers from view `public_impact` (unexpired, reviewed stock; shipping figures hidden until 3+ partners), for the website |

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
pytest              # creates and drops a throwaway database b4p_test_<pid>
pytest -m live      # also syncs from the real feeds
```

Set `B4P_TEST_DATABASE_URL` to test against another server (CI does this).
