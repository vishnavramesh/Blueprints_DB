# Dashboard contract

The internal and external dashboards read and write the database **only**
through the views and functions below (defined at the bottom of
`sql/schema.sql`). Their names, column names and types are stable; anything
else in the schema may change.

Connect with `DATABASE_URL` (local: `postgresql:///b4p_v2`; production: the
Supabase session-pooler URL). If you use psycopg 3 against Supabase, pass
`prepare_threshold=None`.

Data freshness: `python -m b4p sync` (GitHub Actions, every 15 min) copies the
inventory sheet into the tables and imports any *new* requests from the old
requests sheet/feed.

- **Inventory**: the sheet is the source of truth. A quantity set with
  `set_item_quantity()` is kept until someone edits that row's Quantity cell in
  the sheet; then the sheet value wins.
- **Requests**: the database is the source of truth. The sync is insert-only:
  it imports request ids that are not in the database yet (via
  `create_request()` with the original id and timestamp) and never changes a
  request that already exists, so statuses set with `set_request_status()` and
  requests created with `create_request()` are never overwritten. Once the
  external backend serves `/requests` from `dash_requests`, the feed simply
  contains nothing new.

---

## `dash_items` (view) — internal dashboard `Item` model

One row per active inventory row (rows deleted from the sheet are hidden),
ordered by `sheet_row`.

| column | type | notes |
|---|---|---|
| id | TEXT | sheet "Supply ID" if that column exists, else the database `item_id` as text. Stable across syncs. Pass it to `set_item_quantity`. |
| name | TEXT | "Name" cell; blank -> `'Item <sheet_row-1>'` (as the dashboard did) |
| gen_name | TEXT | "General" cell, `''` if blank |
| category | TEXT | one of the 12 approved categories (from the sheet's "Subcategory"/"Subcategories" column); unknown/blank -> `'Misc Non-Medical'`. Note: when that cell is blank the old dashboard parser fell back to the "Category" cell, which holds the box label (`TC/RC`, `ER`, ...); we deliberately do **not** do that. |
| manufacturer | TEXT | canonical name (`'Becton, Dickinson and Company'` -> `'BD'`), `'Unknown'` if blank |
| review_status | TEXT | raw "Review" cell (`'Yes'`/`'No'`/`'review needed'`...), NULL if blank. Dashboards decide what counts as flagged. |
| days_in_warehouse | INT | today - warehouse_date |
| image_url | TEXT | |
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
the sheet's cell text (`''` for an empty cell), except `"_sheet_row"` (INT).
If a dashboard changed the quantity with `set_item_quantity()`, `"Quantity"`
shows that number. `"Review"` is the raw cell text (the external dashboard
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
| "Org Name", "Org Email" | the email used on **that** request (two people submitting under the same org name each keep their own email, so `?email=` filtering is safe) |
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

## `create_request(p_org_name TEXT, p_org_email TEXT, p_items JSONB, p_request_id TEXT DEFAULT NULL, p_requested_at TIMESTAMPTZ DEFAULT NULL) RETURNS TEXT`

Creates the requester (or reuses it by org name), the request and all its lines
in one transaction; returns the request id. Status starts as `'Under Review'`.
The submitter email is stored on the request itself.

`p_items` is a JSON array of `{"item_name": TEXT, "category": TEXT, "quantity": INT >= 1, "review_flag": BOOL}`
(`category` and `review_flag` optional). Raises an error (nothing is written) if
the org name is blank, the array is empty, or a line has no name / quantity < 1.

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
`'Approved'`, `'Shipped'`, `'Delivered'`, `'Cancelled'`, `'Denied'`. Returns
`false` if the request id doesn't exist; raises an error for an unknown status.

```sql
SELECT set_request_status('REQ-1791567333-172', 'Approved');   -- true
SELECT set_request_status('REQ-does-not-exist', 'Shipped');    -- false
```

## `set_item_quantity(p_id TEXT, p_quantity INT) RETURNS TABLE(id TEXT, quantity INT, total_value FLOAT8, sheet_row INT)`

`p_id` is a `dash_items.id`. `p_quantity` may be 0 (the item ran out); a
negative value raises an error. Returns the updated row (zero rows if no active
item has that id); `sheet_row` lets a dashboard write the new value back to the
sheet by row when there is no "Supply ID" column. The sync keeps this quantity
until the sheet's Quantity cell for that row is edited.

```sql
SELECT * FROM set_item_quantity('1', 20);   -- ('1', 20, 0.0, 2)
SELECT * FROM set_item_quantity('1', 0);    -- ('1', 0, 0.0, 2)   ran out
SELECT * FROM set_item_quantity('nope', 5); -- no rows -> respond 404
```

## Changes since the first version

- `set_item_quantity` returns an extra `sheet_row` column; 0 is explicitly allowed.
- `create_request` has two optional trailing parameters (`p_request_id`, `p_requested_at`).
- `dash_requests."Org Email"` is now per request (was per org name).
- The requests sync is insert-only (it used to copy status changes from the feed).
