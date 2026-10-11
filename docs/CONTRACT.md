# Dashboard contract

The internal and external dashboards read and write the database **only**
through the views and functions below (defined at the bottom of
`sql/schema.sql`). Their names, column names and types are stable; anything
else in the schema may change.

Connect with `DATABASE_URL` (local: `postgresql:///b4p_v2`; production: the
Supabase session-pooler URL). If you use psycopg 3 against Supabase, pass
`prepare_threshold=None`.

Data freshness: `python -m b4p sync` (GitHub Actions, every 15 min) copies the
inventory sheet into the tables.

- **Inventory**: the sheet is the source of truth. The sync reads it with a
  Google service account (or the public CSV export if the sheet is link-shared)
  and never from a dashboard's own API. A quantity set with `set_item_quantity()`
  is kept until someone edits that row's Quantity cell in the sheet; then the
  sheet value wins. Safety rails (details in README): if a required column is
  missing/renamed, or fewer than half of the sheet's rows are items we already
  know (wrong tab?), the sync changes nothing; it never lets the active item
  count drop more than 15% below the most we had in the last 24 hours; a row
  with an unusable cell is cleaned or skipped with a warning in
  `sync_run.warnings` instead of failing the run. Ids are stable even for
  copy-pasted rows that share one photo URL.
- **Requests**: the database is the source of truth. Importing requests from the
  old requests sheet/feed is **off by default** (opt in with `REQUESTS_URL` or
  `ORG_REQUESTS_WORKSHEET_TITLE` for a one-time migration). An import is
  insert-only: it adds request ids not in the database yet (via `create_request()`
  with the original id and timestamp) and never changes an existing request.

Errors: functions raise PostgreSQL errors. With psycopg 3:

| error class (SQLSTATE) | meaning | suggested HTTP |
|---|---|---|
| `psycopg.errors.NoDataFound` (P0002) | `set_item_quantity`: no such active item (unknown id or removed from the sheet) | 404 |
| `psycopg.errors.RaiseException` (P0001), message starts with `cannot change request` | `set_request_status`: transition not allowed | 409 |
| `psycopg.errors.RaiseException` (P0001), any other message | invalid input (bad quantity, unknown status, bad items JSON, too long, ...) | 400 |

**Privacy.** Some scanned items are specimen tubes whose labels carry patient
identifiers. Every free-text column of every view goes through
`redact_phi(text)`, which replaces patient IDs (`Pt. ID <number> <code>` →
`Pt. ID [redacted]`), research-subject IDs (`A00-000` → `[id redacted]`), MRNs,
dates of birth, email addresses, US phone numbers and patient names written after
`Patient` / `Pt.` (`Patient: Jane Doe` → `Pt. [name redacted]`). Text is first
stripped of zero-width/bidi characters and NFKC-folded for matching, so
obfuscated forms are caught (text with nothing to redact is returned unchanged).
**Every** text column is covered: Name, General, Notes, Lot Number, Manufacturer,
Category, Subcategory, Review, box label, ... When any of them contained something
to redact, the row's photo link is hidden too (`image_url` NULL in
`dash_items`/`v_inventory`, `"Image URL"` `''` in `dash_supplies`), because the
photo shows the same label. Such rows are flagged for review with
`review_reason` "contains patient identifier" (`v_review_queue`). The tables keep
the original text for HQ; dashboards must only read the views below.

---

## `dash_items` (view) — internal dashboard `Item` model

One row per active inventory row (rows deleted from the sheet are hidden),
ordered by `sheet_row`.

| column | type | notes |
|---|---|---|
| id | TEXT | always the database `item_id` as text (digits). Stable across syncs, never reused. Pass it to `set_item_quantity`. |
| name | TEXT | "Name" cell (personal identifiers redacted); blank -> `'Item <sheet_row-1>'` (as the dashboard did) |
| gen_name | TEXT | "General" cell, `''` if blank |
| category | TEXT | one of the 12 approved categories (from the sheet's "Subcategory"/"Subcategories" column); unknown/blank -> `'Misc Non-Medical'`. Note: when that cell is blank the old dashboard parser fell back to the "Category" cell, which holds the box label (`TC/RC`, `ER`, ...); we deliberately do **not** do that. |
| manufacturer | TEXT | canonical name (`'Becton, Dickinson and Company'` -> `'BD'`), `'Unknown'` if blank |
| review_status | TEXT | raw "Review" cell (`'Yes'`/`'No'`/`'review needed'`...), NULL if blank. Dashboards decide what counts as flagged. |
| days_in_warehouse | INT | today - warehouse_date |
| image_url | TEXT | NULL when the row's name/notes contained a personal identifier (the photo shows it) |
| quantity | INT | |
| unit_value | FLOAT8 | 0 until prices are filled in |
| total_value | FLOAT8 | quantity * unit_value |
| expiration_date | TEXT | `'YYYY-MM-DD'` or NULL |
| warehouse_date | TEXT | `'YYYY-MM-DD'`: the day the sync first saw the row (or derived from a "Days in Warehouse" cell if the sheet has one) |
| pallet_number | INT | 0 if unknown |
| category_box | TEXT | box label (the sheet's box "Category" column, e.g. `'ST/N'`), `'Unassigned'` if unknown |
| box_number | INT | 0 if unknown |
| sheet_row | INT | row number in the sheet (header = 1) |

Example:

```json
{"id": "1", "name": "62697 TELFA PACK UNIVERSITY OF MICHIGAN HOSP", "gen_name": "non-adherent dressing",
 "category": "Wound Care & Bandages", "manufacturer": "Cardinal Health", "review_status": "No",
 "days_in_warehouse": 0, "image_url": "https://drive.google.com/file/d/1pOZb7UjIm_t98gR6Z86DZ84yGCjw59Pd/view?usp=drivesdk",
 "quantity": 16, "unit_value": 0.0, "total_value": 0.0, "expiration_date": "2030-11-07",
 "warehouse_date": "2026-10-09", "pallet_number": 1, "category_box": "ST/N", "box_number": 4, "sheet_row": 2}
```

```sql
SELECT * FROM dash_items;                       -- GET /items
SELECT * FROM dash_items WHERE category = 'Gloves';
```

## `dash_supplies` (view) — external dashboard `/api/supplies` rows

One row per active inventory row, ordered by `"_sheet_row"`. Columns are named
exactly like the sheet headers (quote them in SQL). Every column is TEXT holding
the sheet's cell text (`''` for an empty cell), except `"_sheet_row"` (INT), with
two exceptions that are always normalized: `"Quantity"` is the whole number we
parsed (`'20 (pens)'` → `'20'`; a number set with `set_item_quantity()` shows
here too) and `"Date of Expiration"` is `YYYY-MM-DD` whenever we could parse the
cell (`'01/2024'` → a full date), else the raw cell text. `"Name"`, `"General"` and `"Notes"` have personal
identifiers redacted and `"Image URL"` is `''` for such rows (see Privacy);
`"Review"` is the raw cell text (the external dashboard
treats `Yes` / `review needed` as flagged).

Columns: `"Manufacturer Name"`, `"Category"`, `"Subcategory"`, `"General"`,
`"Name"`, `"Lot Number"`, `"Notes"`, `"Quantity"`, `"Date of Expiration"`,
`"Pallet Number"`, `"Box Number"`, `"Image URL"`, `"Review"`,
`"Days in Warehouse"`, `"_sheet_row"`.

`"Subcategory"` is extra (the live sheet has it: it holds the approved
category, while `"Category"` holds the box label such as `ST/N`).

Example:

```json
{"Manufacturer Name": "Cardinal Health", "Category": "ST/N", "Subcategory": "Wound Care & Bandages",
 "General": "non-adherent dressing", "Name": "62697 TELFA PACK UNIVERSITY OF MICHIGAN HOSP",
 "Lot Number": "675876", "Notes": "Sterile EO, expiration date 2030-11-07, MFG date 2025-11-07, quantity one",
 "Quantity": "16", "Date of Expiration": "2030-11-07", "Pallet Number": "1", "Box Number": "4",
 "Image URL": "https://drive.google.com/file/d/1pOZb7UjIm_t98gR6Z86DZ84yGCjw59Pd/view?usp=drivesdk",
 "Review": "No", "Days in Warehouse": "", "_sheet_row": 2}
```

## `dash_requests` (view) — external dashboard `/requests` rows

One row per request line, ordered by timestamp, request id, line number. All
TEXT, keys exactly as the Render `/requests` payload.

| column | notes |
|---|---|
| "Request ID" | `'REQ-<epoch>-<3 digits>'` |
| "Org Name", "Org Email" | the email given on **that** request only (`''` if none was given; never another request's email), so `?email=` filtering is safe |
| "Item Name", "Category" | as entered by the requester |
| "Quantity Requested" | integer as text |
| "Status" | `'Under Review'`, `'Approved'`, `'Shipped'`, `'Delivered'`, `'Cancelled'` or `'Denied'` |
| "Timestamp" | ISO 8601 UTC, e.g. `'2026-05-23T05:23:06.638914+00:00'` |
| "Review Flag" | `'TRUE'` / `'FALSE'` |

```json
{"Request ID": "REQ-1779513786-450", "Org Name": "Example Clinic", "Org Email": "clinic@example.org",
 "Item Name": "Oropharyngeal Suction Catheter", "Category": "Airway & Oxygen", "Quantity Requested": "1",
 "Status": "Under Review", "Timestamp": "2026-05-23T05:23:06.638914+00:00", "Review Flag": "FALSE"}
```

```sql
SELECT * FROM dash_requests;                                          -- GET /requests
SELECT * FROM dash_requests WHERE lower("Org Email") = lower(%s);     -- GET /requests?email=
```

## `dash_sync_status` (view) — "last synced" indicator

Always exactly one row.

| column | type | notes |
|---|---|---|
| last_success_at | TIMESTAMPTZ | `finished_at` of the latest run with no error whose source is not `unavailable...` and that was not refused by a guard; NULL if none |
| last_run_at | TIMESTAMPTZ | `started_at` of the latest run of any kind; NULL if never run |
| last_run_ok | BOOLEAN | latest run finished without error and actually read the sheet; a sheet-unavailable or guard-refused run is **false**; NULL if never run |
| last_warning_count | INT | warnings recorded by the latest run (0 if none) |
| active_items | INT | inventory rows currently active |
| last_run_refused | BOOLEAN | the latest run was stopped by a safety guard (wrong tab, or too many items missing so removals were skipped); data is frozen/stale. NULL if never run |

## `create_request(p_org_name TEXT, p_org_email TEXT, p_items JSONB, p_request_id TEXT DEFAULT NULL, p_requested_at TIMESTAMPTZ DEFAULT NULL) RETURNS TEXT`

Creates the requester (or reuses it by org name), the request and all its lines
in one transaction; returns the request id. Status starts as `'Under Review'`.
The submitter email is stored on the request itself.

`p_items` is a JSON array of `{"item_name": TEXT, "category": TEXT, "quantity": INT >= 1, "review_flag": BOOL}`
(`category` and `review_flag` optional). Raises an error (nothing is written) if
the org name is blank, the array is empty, or a line has no name / quantity < 1.

Input rules (violations raise and write nothing): org name 1–200 characters;
email ≤ 254; 1–100 items; each item is a JSON object with `item_name` a string
of 1–500 characters, `category` a string ≤ 200 (optional), `quantity` a JSON
**number** that is a whole number 1–999999 (`"2"` or `1.5` are rejected),
`review_flag` a JSON boolean (optional). The request id is generated
collision-safely (concurrent callers never get a duplicate-key error).

Optional, for importing old requests: `p_request_id` uses that id instead of
generating one, `p_requested_at` sets the timestamp (default now). If a request
with `p_request_id` already exists, nothing is written and the id is returned
(idempotent). Dashboards normally call it with three arguments.

```sql
SELECT create_request('Example Clinic', 'clinic@example.org',
  '[{"item_name": "Gauze 4x4", "category": "Wound Care & Bandages", "quantity": 3, "review_flag": false}]');
-- -> 'REQ-1791567333-172'
SELECT create_request('Old Clinic', 'old@example.org', '[{"item_name": "Saline", "quantity": 4}]',
                      'REQ-1700000000-123', '2023-11-14T22:13:20+00:00');   -- import; re-running is a no-op
```

## `set_request_status(p_request_id TEXT, p_status TEXT) RETURNS BOOLEAN`

Accepts display text, case-insensitive: `'Under Review'` (also `'Pending'`),
`'Approved'`, `'Shipped'`, `'Delivered'`, `'Cancelled'`, `'Denied'`.

Allowed changes:
- forward along `Under Review → Approved → Shipped → Delivered` (steps may be skipped);
- `Denied` or `Cancelled` from an open request (`Under Review` or `Approved`);
- setting the status it already has (no-op, returns `true`).

Anything else (going backwards, leaving `Delivered`/`Denied`/`Cancelled`,
cancelling after `Shipped`) raises `cannot change request <id> from <old> to <new>`.
Returns `false` if the request id doesn't exist; raises `unknown status` for an
unrecognised status. (An admin can still fix a mistake with a direct `UPDATE shipment`.)

```sql
SELECT set_request_status('REQ-1791567333-172', 'Approved');   -- true
SELECT set_request_status('REQ-1791567333-172', 'Under Review'); -- ERROR: cannot change request ...
SELECT set_request_status('REQ-does-not-exist', 'Shipped');    -- false
```

## `set_item_quantity(p_id TEXT, p_quantity INT) RETURNS TABLE(id TEXT, quantity INT, total_value FLOAT8, sheet_row INT)`

`p_id` is a `dash_items.id`. `p_quantity` must be 0–999999 (0 = ran out).
Returns exactly one row: the updated item, with `sheet_row` so a dashboard can
write the value back to the sheet. Raises `NoDataFound` (P0002) unless `p_id`
matches exactly one active item (unknown id, or the row was removed from the
sheet); raises (P0001) for a quantity outside 0–999999. The sync keeps this
quantity until the sheet's Quantity cell for that row is edited, including when
the sync is running at the moment you call this.

```sql
SELECT * FROM set_item_quantity('1', 20);   -- ('1', 20, 0.0, 2)
SELECT * FROM set_item_quantity('1', 0);    -- ('1', 0, 0.0, 2)   ran out
SELECT * FROM set_item_quantity('nope', 5); -- ERROR (P0002) -> respond 404
```

## How sheet cells are read (for reference)

- Quantity: first whole number in the cell (`'20 (pens)'` → 20, `'0.5'` → 0,
  `'-'`/`'N/A'` → 0, negative → 0 with a warning, capped at 999999).
- Expiration: `2027-03-15`, `2027/03/15`, `3/15/2027`, `3/15/27` (two-digit
  years are 20xx; `15/03/2027` is read as day/month only when month/day is
  impossible); month only (`2027-03`, `2027.03`, `03/2027`, `03-2027`,
  `Mar 2027`, `March 2027`, `Sept 2027`, `Feb-2027`) → last day of that month;
  year only (`2028`) → Dec 31; an `EXP`/`Expiry:`/`Use by` prefix is ignored;
  years outside 1950–2200 are rejected. A non-empty cell that still can't be
  read (`02/30/2026`, `Marchxyz 2027`) → `expiration_date` NULL **and the row is
  flagged for review** (appears in `v_review_queue`, warning in `sync_run`).
- Prices (`Price per unit`/`Unit Value` if the tab has them): `$1,234.50`,
  `€5,50` (decimal comma) are understood; values over 99,999,999.99 are ignored.
- Box/pallet numbers or "Days in Warehouse" too large to be real are ignored
  (warning); NUL characters are stripped and half-emoji (lone UTF-16
  surrogates) become `\uFFFD` (�).

## Changes since the first version

- `set_item_quantity` returns an extra `sheet_row` column; 0 is explicitly allowed.
- `create_request` has two optional trailing parameters (`p_request_id`, `p_requested_at`).
- `dash_requests."Org Email"` is now per request (was per org name).
- The requests sync is insert-only (it used to copy status changes from the feed).
- Privacy: free text in all views goes through `redact_phi()`; photo links of
  rows with personal identifiers are hidden; `v_review_queue` has a new last
  column `review_reason`. `python -m b4p init` now drops and recreates all views
  and functions (tables untouched), so it upgrades from any schema version; any
  view you build on top of ours is dropped by `init` (CASCADE) and must be recreated.
- Round 3 (hardening): `dash_items.id` is always `item_id` (the "Supply ID"
  branch is gone); `set_item_quantity` raises instead of returning zero rows
  and rejects quantities over 999999; `dash_requests."Org Email"` no longer
  falls back to the org's first email; `set_request_status` only allows the
  transitions above; `create_request` validates types/lengths (max 100 lines);
  requests import is opt-in; month-only expiration dates mean the last day of
  the month and two-digit years are 20xx.
