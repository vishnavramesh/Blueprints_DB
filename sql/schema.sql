-- Blueprints for Pangaea (B4P) inventory database
-- PostgreSQL 15+ (works on Supabase).
--
-- Apply with:   python -m b4p init        (or: psql "$DATABASE_URL" -f sql/schema.sql)
-- Safe to re-run: every statement is CREATE ... IF NOT EXISTS / CREATE OR REPLACE
-- / INSERT ... ON CONFLICT DO NOTHING.
--
-- Where the data comes from:
--   inventory sheet row  ->  inventory_item (one row per sheet row / photo)
--   org requests feed    ->  requester + shipment + shipment_item
-- The "dash_*" views and the three functions at the bottom are the contract the
-- internal and external dashboards read/write (see docs/CONTRACT.md).

BEGIN;

CREATE EXTENSION IF NOT EXISTS citext;   -- case-insensitive text (names, aliases)

-- ---------------------------------------------------------------------------
-- Lookup tables
-- ---------------------------------------------------------------------------

-- The 12 approved categories. Anything else maps to 'Misc Non-Medical'.
CREATE TABLE IF NOT EXISTS category (
    category_id BIGINT PRIMARY KEY,
    name        CITEXT NOT NULL UNIQUE
);
INSERT INTO category (category_id, name) VALUES
    ( 1, 'IV Supplies'),
    ( 2, 'Misc Surgical'),
    ( 3, 'Wound Care & Bandages'),
    ( 4, 'Needles & Syringes'),
    ( 5, 'Catheters & Briefs'),
    ( 6, 'Airway & Oxygen'),
    ( 7, 'Vitals & Home Health'),
    ( 8, 'Gloves'),
    ( 9, 'Apparel (Gowns, Shoe Covers, etc.)'),
    (10, 'Masks & Face Shields'),
    (11, 'Sutures'),
    (12, 'Misc Non-Medical')
ON CONFLICT DO NOTHING;

-- Other spellings seen in the sheet -> approved category. Add rows here instead
-- of editing code.
CREATE TABLE IF NOT EXISTS category_alias (
    alias       CITEXT PRIMARY KEY,
    category_id BIGINT NOT NULL REFERENCES category(category_id) ON DELETE CASCADE
);
INSERT INTO category_alias (alias, category_id) VALUES
    ('unspecified', 12), ('uncategorized', 12), ('medical supplies', 12)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS manufacturer (
    manufacturer_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            CITEXT NOT NULL UNIQUE
);

-- 'Becton, Dickinson and Company' -> 'BD', etc.
CREATE TABLE IF NOT EXISTS manufacturer_alias (
    alias           CITEXT PRIMARY KEY,
    manufacturer_id BIGINT NOT NULL REFERENCES manufacturer(manufacturer_id) ON DELETE CASCADE
);
INSERT INTO manufacturer (name)
VALUES ('BD'), ('Cardinal Health'), ('Medline'), ('Owens & Minor')
ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id)
SELECT a.alias, m.manufacturer_id
  FROM (VALUES ('becton, dickinson and company', 'BD'), ('becton dickinson', 'BD'),
               ('cardinalhealth', 'Cardinal Health'), ('cardinal health 200, llc', 'Cardinal Health'),
               ('medline industries', 'Medline'), ('medline industries, lp', 'Medline'),
               ('owens & minor, inc.', 'Owens & Minor')) AS a(alias, canon)
  JOIN manufacturer m ON m.name = a.canon
ON CONFLICT DO NOTHING;

-- Box labels (the sheet's box "Category" column: 'ST/N', 'TC/RC', 'ER', ...).
-- New labels found in the sheet are added automatically by the sync.
CREATE TABLE IF NOT EXISTS box_type (
    code        CITEXT PRIMARY KEY,
    description TEXT
);
INSERT INTO box_type (code, description) VALUES
    ('TC/RC',   'Trach care / respiratory care'),
    ('ST/N',    'Syringes / tubes / needles'),
    ('DME',     'Durable medical equipment'),
    ('ER',      'Emergency room'),
    ('MISC',    'Miscellaneous'),
    ('PH/PPE',  'Pharmacy / personal protective equipment'),
    ('IV-PROC', 'IV / procedural'),
    ('PT-CARE', 'Patient care'),
    ('WOUND',   'Wound care'),
    ('LEGACY',  'Old boxes labeled by number only')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS box_type_alias (
    alias CITEXT PRIMARY KEY,
    code  CITEXT NOT NULL REFERENCES box_type(code) ON UPDATE CASCADE ON DELETE CASCADE
);
INSERT INTO box_type_alias (alias, code) VALUES
    ('STN', 'ST/N'), ('TCRC', 'TC/RC'), ('TC/RC/', 'TC/RC'), ('TCRC/', 'TC/RC')
ON CONFLICT DO NOTHING;

-- A physical box: label + number on a pallet. Any part may be unknown.
CREATE TABLE IF NOT EXISTS box (
    box_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    box_type      CITEXT REFERENCES box_type(code) ON UPDATE CASCADE,
    box_number    INTEGER CHECK (box_number >= 0),
    pallet_number INTEGER CHECK (pallet_number >= 0),
    UNIQUE NULLS NOT DISTINCT (box_type, box_number, pallet_number)
);

-- ---------------------------------------------------------------------------
-- Catalog + inventory
-- ---------------------------------------------------------------------------

-- One row per distinct product (manufacturer + name). Groups lots together and
-- holds a unit price if someone fills it in.
CREATE TABLE IF NOT EXISTS supply (
    supply_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    manufacturer_id BIGINT REFERENCES manufacturer(manufacturer_id) ON DELETE RESTRICT,
    name            TEXT NOT NULL,
    unit_value_usd  NUMERIC(10,2) CHECK (unit_value_usd >= 0),
    UNIQUE NULLS NOT DISTINCT (manufacturer_id, name)
);

-- One row per inventory-sheet row (one photo/scan of a lot of supplies).
CREATE TABLE IF NOT EXISTS inventory_item (
    item_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- identity in the sheet: the row's Image URL (or a content hash if it has none)
    source_key      TEXT NOT NULL UNIQUE,
    sheet_row       INTEGER,                 -- 1 = header row, so data starts at 2
    -- what the row says, normalized
    supply_id       BIGINT REFERENCES supply(supply_id) ON DELETE SET NULL,
    raw_name        TEXT NOT NULL DEFAULT '',  -- "Name" cell as typed
    general_name    TEXT,                      -- "General"
    category_id     BIGINT NOT NULL DEFAULT 12 REFERENCES category(category_id),
    manufacturer_id BIGINT REFERENCES manufacturer(manufacturer_id),
    box_id          BIGINT REFERENCES box(box_id) ON DELETE SET NULL,
    quantity        INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0 AND quantity < 1000000),
    unit_value      NUMERIC(10,2) CHECK (unit_value >= 0),   -- "Price per unit"/"Unit Value" cell
    lot_number      TEXT,
    expiration_date DATE,                      -- NULL = none printed
    warehouse_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    image_url       TEXT,
    notes           TEXT,
    review          TEXT,                      -- "Review" cell ('Yes'/'No'/...)
    flagged         BOOLEAN NOT NULL DEFAULT false,  -- needs a human look
    review_reason   TEXT,                      -- why ('contains patient identifier; ...')
    reviewed_at     TIMESTAMPTZ,               -- a human resolved the flag
    -- sync bookkeeping
    raw             JSONB NOT NULL DEFAULT '{}',  -- the whole sheet row, header -> cell text
    source_hash     TEXT,                      -- md5 of raw + sheet_row; changes when the row is edited
    quantity_set_at TIMESTAMPTZ,               -- set by set_item_quantity(); sync keeps that
                                               -- quantity until the sheet's Quantity cell changes
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at      TIMESTAMPTZ                -- row vanished from the sheet; kept for history
);
ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS review_reason TEXT;
CREATE INDEX IF NOT EXISTS inventory_item_supply ON inventory_item(supply_id);
CREATE INDEX IF NOT EXISTS inventory_item_box    ON inventory_item(box_id);
CREATE INDEX IF NOT EXISTS inventory_item_active ON inventory_item(sheet_row) WHERE removed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Requests (organizations asking for supplies)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS requester (
    requester_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_name     CITEXT NOT NULL UNIQUE,
    org_email    CITEXT
);

-- One request ('REQ-...') = one shipment.
CREATE TABLE IF NOT EXISTS shipment (
    shipment_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id   TEXT NOT NULL UNIQUE,
    requester_id BIGINT NOT NULL REFERENCES requester(requester_id) ON DELETE RESTRICT,
    org_email    CITEXT,        -- email of whoever submitted THIS request
    status       TEXT NOT NULL DEFAULT 'Under Review'
                 CHECK (status IN ('Under Review','Approved','Shipped','Delivered','Cancelled','Denied')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 'feed' = imported by the sync from the old requests sheet/feed (insert-only);
    -- 'app'  = submitted through a dashboard via create_request().
    -- Either way, once a request is in the database the database is its source of truth.
    source       TEXT NOT NULL DEFAULT 'app' CHECK (source IN ('feed','app')),
    notes        TEXT
);
-- upgrades for databases created by an earlier version of this file
ALTER TABLE shipment ADD COLUMN IF NOT EXISTS org_email CITEXT;
ALTER TABLE shipment DROP COLUMN IF EXISTS feed_status;
CREATE INDEX IF NOT EXISTS shipment_requester ON shipment(requester_id);

CREATE TABLE IF NOT EXISTS shipment_item (
    shipment_item_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shipment_id        BIGINT NOT NULL REFERENCES shipment(shipment_id) ON DELETE CASCADE,
    line_no            INTEGER NOT NULL CHECK (line_no >= 1),
    item_name          TEXT NOT NULL,
    category           TEXT NOT NULL DEFAULT '',   -- as entered by the requester
    quantity_requested INTEGER NOT NULL CHECK (quantity_requested >= 0),
    review_flag        BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (shipment_id, line_no)
);

-- ---------------------------------------------------------------------------
-- Sync bookkeeping: one row per `python -m b4p sync`
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_run (
    run_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    source         TEXT,                 -- which feeds were used (sheet tab / fallback)
    rows_seen      INTEGER NOT NULL DEFAULT 0,
    items_added    INTEGER NOT NULL DEFAULT 0,
    items_updated  INTEGER NOT NULL DEFAULT 0,
    items_removed  INTEGER NOT NULL DEFAULT 0,
    rows_skipped   INTEGER NOT NULL DEFAULT 0,
    requests_added INTEGER NOT NULL DEFAULT 0,
    lines_added    INTEGER NOT NULL DEFAULT 0,
    active_items   INTEGER,              -- active inventory rows after the run
    warnings       JSONB NOT NULL DEFAULT '[]',
    error          TEXT
);
ALTER TABLE sync_run DROP COLUMN IF EXISTS status_changes;
ALTER TABLE sync_run ADD COLUMN IF NOT EXISTS active_items INTEGER;  -- requests are insert-only now

-- ---------------------------------------------------------------------------
-- Everyday views
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Derived objects (views + functions) are dropped and recreated on every init,
-- so upgrading from any older/newer version works even when a view's columns
-- changed (CREATE OR REPLACE VIEW can't drop or reorder columns). Tables above
-- are never dropped. All of this runs in one transaction.
-- NOTE: CASCADE also drops any view someone else built on top of these.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS dash_requests, dash_supplies, dash_items,
                    v_review_queue, v_expiring, v_kpis, v_inventory CASCADE;
DROP FUNCTION IF EXISTS set_item_quantity(TEXT, INT) CASCADE;
DROP FUNCTION IF EXISTS set_request_status(TEXT, TEXT) CASCADE;
DROP FUNCTION IF EXISTS create_request(TEXT, TEXT, JSONB) CASCADE;
DROP FUNCTION IF EXISTS create_request(TEXT, TEXT, JSONB, TEXT, TIMESTAMPTZ) CASCADE;
DROP FUNCTION IF EXISTS b4p_status(TEXT) CASCADE;
DROP FUNCTION IF EXISTS b4p_iso_utc(TIMESTAMPTZ) CASCADE;
DROP FUNCTION IF EXISTS b4p_has_phi(TEXT[]) CASCADE;
DROP FUNCTION IF EXISTS redact_phi(TEXT) CASCADE;
DROP FUNCTION IF EXISTS b4p_patient_id_pattern() CASCADE;
DROP FUNCTION IF EXISTS b4p_subject_id_pattern() CASCADE;

-- ---------------------------------------------------------------------------
-- Privacy. Some scanned items are specimen tubes whose labels carry patient
-- identifiers ("Pt. ID <number> <code>"). The tables keep the original text
-- for HQ, but every view below shows free text through redact_phi(), and hides
-- the photo link when the row's name/notes had something to redact (the photo
-- shows the same label). The sync also flags such rows for review.
-- ---------------------------------------------------------------------------

-- Patient-identifier pattern; mirrors PATIENT_ID_RE in b4p/normalize.py.
-- "Pt." / "Patient", optional "ID" / "#" / "No.", then 1-4 tokens that are
-- initials (1-3 capitals) or contain a digit.
CREATE OR REPLACE FUNCTION b4p_patient_id_pattern() RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT '\y(?:[Pp][Tt]\y\.?|[Pp][Aa][Tt][Ii][Ee][Nn][Tt]\y)(?:\s*(?:[Ii][Dd]\y|#|[Nn][Oo]\y\.?))?[\s:#,.]*'
        || '(?:(?!ID\y)[A-Z]{1,3}\y|[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*)'
        || '(?:[\s,]+(?:(?!ID\y)[A-Z]{1,3}\y|[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*)){0,3}'
$f$;

-- Research-subject / specimen ID on trial sample tubes, e.g. "A00-000"
-- (letter, 2 digits, dash, 3 digits). Mirrors SUBJECT_ID_RE in normalize.py.
CREATE OR REPLACE FUNCTION b4p_subject_id_pattern() RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT '\y[A-Z][0-9]{2}-[0-9]{3}\y'
$f$;

-- Text with personal identifiers replaced: patient IDs, research-subject IDs,
-- MRNs, dates of birth, email addresses and US phone numbers.
CREATE OR REPLACE FUNCTION redact_phi(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $f$
    SELECT regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(t,
        '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[email redacted]', 'g'),
        '\y(?:[Mm][Rr][Nn]|[Mm][Rr]#|[Mm]ed(?:ical)?\s*[Rr]ec(?:ord)?\s*(?:#|[Nn]o\.?|[Nn]umber))[\s:#.]*[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*',
        'MRN [redacted]', 'g'),
        '\y(?:[Dd][Oo][Bb]|[Dd]\.[Oo]\.[Bb]\.?|[Dd]ate of [Bb]irth)[\s:]*[0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4}',
        'DOB [redacted]', 'g'),
        b4p_patient_id_pattern(), 'Pt. ID [redacted]', 'g'),
        b4p_subject_id_pattern(), '[id redacted]', 'g'),
        '(?:\+?1[\s.-]?)?(?:\([0-9]{3}\)\s?|\y[0-9]{3}[\s.-])[0-9]{3}[\s.-][0-9]{4}\y', '[phone redacted]', 'g')
$f$;

-- true if redact_phi would change any of the texts
CREATE OR REPLACE FUNCTION b4p_has_phi(VARIADIC t TEXT[]) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT EXISTS (SELECT 1 FROM unnest(t) x WHERE x IS NOT NULL AND redact_phi(x) <> x)
$f$;

-- Human-friendly inventory (active rows only). Free text is redacted.
CREATE OR REPLACE VIEW v_inventory AS
SELECT i.item_id, i.sheet_row,
       redact_phi(COALESCE(s.name, NULLIF(i.raw_name, ''))) AS name,
       redact_phi(i.general_name) AS general_name,
       m.name::TEXT AS manufacturer,
       c.name::TEXT AS category,
       i.quantity, i.lot_number, i.expiration_date,
       (i.expiration_date < CURRENT_DATE) AS is_expired,
       i.warehouse_date,
       CURRENT_DATE - i.warehouse_date AS days_in_warehouse,
       b.box_type::TEXT AS box_type, b.box_number, b.pallet_number,
       i.flagged, i.review,
       CASE WHEN b4p_has_phi(i.raw_name, i.notes) THEN NULL ELSE i.image_url END AS image_url,
       redact_phi(i.notes) AS notes
FROM inventory_item i
LEFT JOIN supply s       ON s.supply_id = i.supply_id
LEFT JOIN manufacturer m ON m.manufacturer_id = i.manufacturer_id
JOIN category c          ON c.category_id = i.category_id
LEFT JOIN box b          ON b.box_id = i.box_id
WHERE i.removed_at IS NULL;

-- Headline numbers.
CREATE OR REPLACE VIEW v_kpis AS
SELECT count(*)                                         AS total_entries,
       COALESCE(sum(i.quantity), 0)                     AS total_raw_items,
       COALESCE(sum(i.quantity * COALESCE(i.unit_value, s.unit_value_usd, 0)), 0) AS total_value,
       count(DISTINCT b.pallet_number)                  AS pallet_count,
       count(DISTINCT i.box_id)                         AS box_count,
       count(*) FILTER (WHERE i.flagged AND i.reviewed_at IS NULL) AS awaiting_review
FROM inventory_item i
LEFT JOIN supply s ON s.supply_id = i.supply_id
LEFT JOIN box b    ON b.box_id = i.box_id
WHERE i.removed_at IS NULL;

-- Expired or expiring within 90 days.
CREATE OR REPLACE VIEW v_expiring AS
SELECT v.*, v.expiration_date - CURRENT_DATE AS days_left
FROM v_inventory v
WHERE v.expiration_date < CURRENT_DATE + 90 AND v.quantity > 0;

-- Rows a human should look at (unreadable scan, Review = Yes, ...).
CREATE OR REPLACE VIEW v_review_queue AS
SELECT v.*, i.review_reason FROM v_inventory v
JOIN inventory_item i USING (item_id)
WHERE i.flagged AND i.reviewed_at IS NULL;

-- ===========================================================================
-- DASHBOARD CONTRACT  (docs/CONTRACT.md) -- do not rename/reshape casually
-- ===========================================================================

-- Internal dashboard: shaped like its `Item` model.
CREATE OR REPLACE VIEW dash_items AS
SELECT i.item_id::TEXT                                                   AS id,
       redact_phi(COALESCE(NULLIF(i.raw_name, ''), 'Item ' || (i.sheet_row - 1))) AS name,
       COALESCE(redact_phi(i.general_name), '')                          AS gen_name,
       c.name::TEXT                                                      AS category,
       COALESCE(m.name::TEXT, 'Unknown')                                 AS manufacturer,
       NULLIF(i.review, '')                                              AS review_status,
       (CURRENT_DATE - i.warehouse_date)::INT                            AS days_in_warehouse,
       CASE WHEN b4p_has_phi(i.raw_name, i.notes) THEN NULL ELSE i.image_url END AS image_url,
       i.quantity,
       COALESCE(i.unit_value, s.unit_value_usd, 0)::FLOAT8               AS unit_value,
       (i.quantity * COALESCE(i.unit_value, s.unit_value_usd, 0))::FLOAT8 AS total_value,
       to_char(i.expiration_date, 'YYYY-MM-DD')                          AS expiration_date,
       to_char(i.warehouse_date, 'YYYY-MM-DD')                           AS warehouse_date,
       COALESCE(b.pallet_number, 0)                                      AS pallet_number,
       COALESCE(b.box_type::TEXT, 'Unassigned')                          AS category_box,
       COALESCE(b.box_number, 0)                                         AS box_number,
       i.sheet_row
FROM inventory_item i
JOIN category c          ON c.category_id = i.category_id
LEFT JOIN manufacturer m ON m.manufacturer_id = i.manufacturer_id
LEFT JOIN supply s       ON s.supply_id = i.supply_id
LEFT JOIN box b          ON b.box_id = i.box_id
WHERE i.removed_at IS NULL
ORDER BY i.sheet_row, i.item_id;

-- External dashboard: same keys/cell text as its /api/supplies (Google Sheet) rows.
CREATE OR REPLACE VIEW dash_supplies AS
SELECT COALESCE(i.raw ->> 'Manufacturer Name', '')  AS "Manufacturer Name",
       COALESCE(i.raw ->> 'Category', '')           AS "Category",
       COALESCE(i.raw ->> 'Subcategory', i.raw ->> 'Subcategories', '') AS "Subcategory",
       COALESCE(redact_phi(i.raw ->> 'General'), '') AS "General",
       COALESCE(redact_phi(i.raw ->> 'Name'), '')   AS "Name",
       COALESCE(i.raw ->> 'Lot Number', '')         AS "Lot Number",
       COALESCE(redact_phi(i.raw ->> 'Notes'), '')  AS "Notes",
       CASE WHEN i.quantity_set_at IS NOT NULL THEN i.quantity::TEXT
            ELSE COALESCE(i.raw ->> 'Quantity', '') END AS "Quantity",
       COALESCE(i.raw ->> 'Date of Expiration', '') AS "Date of Expiration",
       COALESCE(i.raw ->> 'Pallet Number', '')      AS "Pallet Number",
       COALESCE(i.raw ->> 'Box Number', '')         AS "Box Number",
       CASE WHEN b4p_has_phi(i.raw ->> 'Name', i.raw ->> 'Notes') THEN ''
            ELSE COALESCE(i.raw ->> 'Image URL', '') END AS "Image URL",
       COALESCE(i.raw ->> 'Review', '')             AS "Review",
       COALESCE(i.raw ->> 'Days in Warehouse', '')  AS "Days in Warehouse",
       i.sheet_row                                  AS "_sheet_row"
FROM inventory_item i
WHERE i.removed_at IS NULL
ORDER BY i.sheet_row, i.item_id;

-- External dashboard: same keys as its /requests rows (one row per request line).
CREATE OR REPLACE FUNCTION b4p_iso_utc(ts TIMESTAMPTZ) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    -- matches Python's datetime.isoformat() for a UTC time
    SELECT to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN to_char(ts AT TIME ZONE 'UTC', 'US') = '000000' THEN ''
                ELSE '.' || to_char(ts AT TIME ZONE 'UTC', 'US') END
        || '+00:00'
$$;

CREATE OR REPLACE VIEW dash_requests AS
SELECT sh.request_id                         AS "Request ID",
       r.org_name::TEXT                      AS "Org Name",
       COALESCE(sh.org_email::TEXT, '')      AS "Org Email",   -- this request's email only
       redact_phi(si.item_name)              AS "Item Name",
       si.category                           AS "Category",
       si.quantity_requested::TEXT           AS "Quantity Requested",
       sh.status                             AS "Status",
       b4p_iso_utc(sh.requested_at)          AS "Timestamp",
       CASE WHEN si.review_flag THEN 'TRUE' ELSE 'FALSE' END AS "Review Flag"
FROM shipment sh
JOIN requester r      ON r.requester_id = sh.requester_id
JOIN shipment_item si ON si.shipment_id = sh.shipment_id
ORDER BY sh.requested_at, sh.request_id, si.line_no;

-- 'approved' / 'APPROVED' / 'Pending' ... -> canonical display status (NULL if unknown)
CREATE OR REPLACE FUNCTION b4p_status(p TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE lower(btrim(p))
        WHEN 'under review' THEN 'Under Review'
        WHEN 'under_review' THEN 'Under Review'
        WHEN 'pending'      THEN 'Under Review'
        WHEN 'requested'    THEN 'Under Review'
        WHEN 'approved'     THEN 'Approved'
        WHEN 'shipped'      THEN 'Shipped'
        WHEN 'delivered'    THEN 'Delivered'
        WHEN 'cancelled'    THEN 'Cancelled'
        WHEN 'canceled'     THEN 'Cancelled'
        WHEN 'denied'       THEN 'Denied'
    END
$$;

-- Create an org request with its lines in one go. Returns 'REQ-<epoch>-<3 digits>'.
-- p_items: [{"item_name": "...", "category": "...", "quantity": 2, "review_flag": false}, ...]
--   item_name: string, 1-500 chars      category: string <= 200 chars (optional)
--   quantity:  whole number 1..999999    review_flag: true/false (optional)
--   at most 100 lines; org name 1-200 chars; email <= 254 chars.
-- p_request_id / p_requested_at (optional) are for importing old requests: if a
-- request with that id already exists, nothing is written and the id is returned.
CREATE OR REPLACE FUNCTION create_request(p_org_name TEXT, p_org_email TEXT, p_items JSONB,
                                          p_request_id TEXT DEFAULT NULL,
                                          p_requested_at TIMESTAMPTZ DEFAULT NULL)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    v_org       TEXT := btrim(COALESCE(p_org_name, ''));
    v_email     TEXT := NULLIF(btrim(COALESCE(p_org_email, '')), '');
    v_req_id    TEXT := NULLIF(btrim(COALESCE(p_request_id, '')), '');
    v_requester BIGINT;
    v_shipment  BIGINT;
    v_item      JSONB;
    v_line      INTEGER := 0;
    v_name      TEXT;
    v_qty       NUMERIC;
BEGIN
    IF v_req_id IS NOT NULL AND EXISTS (SELECT 1 FROM shipment WHERE request_id = v_req_id) THEN
        RETURN v_req_id;   -- already imported
    END IF;
    IF v_org = '' OR length(v_org) > 200 THEN
        RAISE EXCEPTION 'org name must be 1-200 characters';
    END IF;
    IF length(v_email) > 254 THEN
        RAISE EXCEPTION 'email must be at most 254 characters';
    END IF;
    IF length(v_req_id) > 64 THEN
        RAISE EXCEPTION 'request id must be at most 64 characters';
    END IF;
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'p_items must be a non-empty JSON array';
    END IF;
    IF jsonb_array_length(p_items) > 100 THEN
        RAISE EXCEPTION 'at most 100 items per request';
    END IF;
    -- validate every line before writing anything
    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items) LOOP
        v_line := v_line + 1;
        IF jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'item % must be a JSON object', v_line;
        END IF;
        IF jsonb_typeof(v_item -> 'item_name') IS DISTINCT FROM 'string'
           OR btrim(v_item ->> 'item_name') = '' OR length(v_item ->> 'item_name') > 500 THEN
            RAISE EXCEPTION 'item %: item_name must be a string of 1-500 characters', v_line;
        END IF;
        IF COALESCE(jsonb_typeof(v_item -> 'category'), 'null') NOT IN ('string', 'null')
           OR length(v_item ->> 'category') > 200 THEN
            RAISE EXCEPTION 'item %: category must be a string of at most 200 characters', v_line;
        END IF;
        IF COALESCE(jsonb_typeof(v_item -> 'review_flag'), 'null') NOT IN ('boolean', 'null') THEN
            RAISE EXCEPTION 'item %: review_flag must be true or false', v_line;
        END IF;
        IF jsonb_typeof(v_item -> 'quantity') IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION 'item %: quantity must be a number', v_line;
        END IF;
        v_qty := (v_item ->> 'quantity')::NUMERIC;
        IF v_qty <> trunc(v_qty) OR v_qty < 1 OR v_qty > 999999 THEN
            RAISE EXCEPTION 'item %: quantity must be a whole number from 1 to 999999', v_line;
        END IF;
    END LOOP;

    -- the requester row keeps the first email seen; each request keeps its own
    INSERT INTO requester (org_name, org_email) VALUES (v_org, v_email)
    ON CONFLICT (org_name) DO UPDATE SET org_email = COALESCE(requester.org_email, EXCLUDED.org_email)
    RETURNING requester_id INTO v_requester;

    IF v_req_id IS NOT NULL THEN
        INSERT INTO shipment (request_id, requester_id, org_email, status, requested_at, source)
        VALUES (v_req_id, v_requester, v_email, 'Under Review', COALESCE(p_requested_at, now()), 'app')
        ON CONFLICT (request_id) DO NOTHING
        RETURNING shipment_id INTO v_shipment;
        IF v_shipment IS NULL THEN
            RETURN v_req_id;   -- imported concurrently by someone else
        END IF;
    ELSE
        LOOP  -- same id format as the external dashboard; retry on collision
            v_req_id := 'REQ-' || floor(extract(epoch FROM clock_timestamp()))::BIGINT
                        || '-' || lpad(floor(random() * 1000)::INT::TEXT, 3, '0');
            INSERT INTO shipment (request_id, requester_id, org_email, status, requested_at, source)
            VALUES (v_req_id, v_requester, v_email, 'Under Review', COALESCE(p_requested_at, now()), 'app')
            ON CONFLICT (request_id) DO NOTHING
            RETURNING shipment_id INTO v_shipment;
            EXIT WHEN v_shipment IS NOT NULL;
        END LOOP;
    END IF;

    v_line := 0;
    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items) LOOP
        v_line := v_line + 1;
        INSERT INTO shipment_item (shipment_id, line_no, item_name, category,
                                   quantity_requested, review_flag)
        VALUES (v_shipment, v_line, btrim(v_item ->> 'item_name'),
                COALESCE(v_item ->> 'category', ''), (v_item ->> 'quantity')::NUMERIC::INTEGER,
                COALESCE((v_item ->> 'review_flag')::BOOLEAN, false));
    END LOOP;

    RETURN v_req_id;
END $$;

-- Change a request's status. Accepts display text ('Approved', 'shipped', ...).
-- Allowed moves (anything else raises an error):
--   forward along  Under Review -> Approved -> Shipped -> Delivered  (steps may be skipped)
--   Denied / Cancelled from an open request (Under Review or Approved)
--   setting the status it already has (no-op, returns true)
-- Returns false if the request id doesn't exist. To undo a mistake, an admin
-- can UPDATE shipment SET status = ... directly.
CREATE OR REPLACE FUNCTION set_request_status(p_request_id TEXT, p_status TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE
    v_new   TEXT := b4p_status(p_status);
    v_old   TEXT;
    v_order CONSTANT TEXT[] := ARRAY['Under Review', 'Approved', 'Shipped', 'Delivered'];
BEGIN
    IF v_new IS NULL THEN
        RAISE EXCEPTION 'unknown status %', p_status;
    END IF;
    SELECT status INTO v_old FROM shipment WHERE request_id = btrim(p_request_id) FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF v_new = v_old THEN
        RETURN true;
    END IF;
    IF NOT (
        (v_new = ANY (v_order) AND v_old = ANY (v_order)
             AND array_position(v_order, v_new) > array_position(v_order, v_old))
        OR (v_new IN ('Denied', 'Cancelled') AND v_old IN ('Under Review', 'Approved'))
    ) THEN
        RAISE EXCEPTION 'cannot change request % from % to %', btrim(p_request_id), v_old, v_new;
    END IF;
    UPDATE shipment SET status = v_new WHERE request_id = btrim(p_request_id);
    RETURN true;
END $$;

-- Set an item's quantity (p_id = dash_items.id). 0 is allowed (ran out).
-- Raises (SQLSTATE P0002 no_data_found) if p_id is not exactly one active item,
-- e.g. unknown id or an item removed from the sheet; raises (P0001) for a
-- quantity outside 0..999999. Returns the updated row, including sheet_row so
-- a dashboard can write the value back to the sheet.
CREATE FUNCTION set_item_quantity(p_id TEXT, p_quantity INT)
RETURNS TABLE (id TEXT, quantity INT, total_value FLOAT8, sheet_row INT) LANGUAGE plpgsql AS $$
DECLARE
    v_item    BIGINT;
    v_removed TIMESTAMPTZ;
BEGIN
    IF p_quantity IS NULL OR p_quantity < 0 OR p_quantity > 999999 THEN
        RAISE EXCEPTION 'quantity must be between 0 and 999999';
    END IF;
    IF btrim(COALESCE(p_id, '')) !~ '^\d{1,18}$' THEN
        RAISE EXCEPTION 'item % not found', p_id USING ERRCODE = 'no_data_found';
    END IF;
    SELECT i.item_id, i.removed_at INTO v_item, v_removed
      FROM inventory_item i WHERE i.item_id = btrim(p_id)::BIGINT FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'item % not found', p_id USING ERRCODE = 'no_data_found';
    END IF;
    IF v_removed IS NOT NULL THEN
        RAISE EXCEPTION 'item % was removed from the sheet', p_id USING ERRCODE = 'no_data_found';
    END IF;
    UPDATE inventory_item i
       SET quantity = p_quantity, quantity_set_at = clock_timestamp(), updated_at = now()
     WHERE i.item_id = v_item;
    RETURN QUERY
    SELECT i.item_id::TEXT, i.quantity,
           (i.quantity * COALESCE(i.unit_value, s.unit_value_usd, 0))::FLOAT8, i.sheet_row
      FROM inventory_item i LEFT JOIN supply s ON s.supply_id = i.supply_id
     WHERE i.item_id = v_item;
END $$;

-- upgrade: the "Supply ID" id branch was removed (no tab has that column)
ALTER TABLE inventory_item DROP COLUMN IF EXISTS sheet_supply_id;

COMMIT;
