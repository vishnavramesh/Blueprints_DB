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
-- Optional stable reference to the inventory row the requester picked (dash_items.id).
-- inventory_item.item_id survives every sync (rows removed from the sheet are only marked
-- removed_at, never deleted), unlike the item name, which requesters retype or abbreviate.
ALTER TABLE shipment_item ADD COLUMN IF NOT EXISTS item_id BIGINT REFERENCES inventory_item(item_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS shipment_item_item ON shipment_item(item_id);

-- ---------------------------------------------------------------------------
-- Ops shipments (internal dashboard "Shipments" tab): outgoing shipments HQ plans,
-- pulls, consolidates and ships. Not the same thing as `shipment` above, which is
-- an org request. Written only through create_shipment() & co. at the bottom.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dash_shipment (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    recipient          TEXT NOT NULL DEFAULT '',
    shipment_type      TEXT NOT NULL DEFAULT '',
    shipping_method    TEXT NOT NULL DEFAULT '',
    blueprints_contact TEXT NOT NULL DEFAULT '',
    stage              TEXT NOT NULL DEFAULT 'planned'
                       CHECK (stage IN ('planned', 'pulling', 'consolidated', 'shipped', 'delivered')),
    ship_date          DATE,
    delivered_date     DATE,
    request_ids        TEXT[] NOT NULL DEFAULT '{}',   -- "Request ID"s from the external request site
    notes              TEXT NOT NULL DEFAULT '',
    -- 'non_inventory': shipped without using inventory rows (no lines), only a dollar value
    kind               TEXT NOT NULL DEFAULT 'inventory' CHECK (kind IN ('inventory', 'non_inventory')),
    unit_count         INTEGER CHECK (unit_count >= 0),
    value              NUMERIC(12,2) CHECK (value >= 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dash_shipment_stage ON dash_shipment(stage);

CREATE TABLE IF NOT EXISTS dash_shipment_line (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shipment_id   TEXT NOT NULL REFERENCES dash_shipment(id) ON DELETE CASCADE,
    item_id       BIGINT REFERENCES inventory_item(item_id),   -- dash_items.id; NULL = free-text line
    description   TEXT NOT NULL DEFAULT '',   -- free-text line: its name; inventory line: name snapshot
    lot_snapshot  TEXT,                       -- free-text / history line: lot and expiry as typed
    exp_snapshot  DATE,
    unit_price    NUMERIC(10,2) CHECK (unit_price >= 0),   -- price at the time; NULL = use the item's current price
    quantity      INTEGER NOT NULL CHECK (quantity > 0 AND quantity < 1000000),
    picked        BOOLEAN NOT NULL DEFAULT false,
    consolidated  BOOLEAN NOT NULL DEFAULT false,
    taken         BOOLEAN NOT NULL DEFAULT false,   -- stock already taken out of inventory (shipment shipped)
    UNIQUE (shipment_id, item_id),
    CHECK (item_id IS NOT NULL OR description <> '')
);
CREATE INDEX IF NOT EXISTS dash_shipment_line_item ON dash_shipment_line(item_id);

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
ALTER TABLE sync_run ADD COLUMN IF NOT EXISTS refused TEXT;  -- set when a safety guard made the run change nothing / skip removals
ALTER TABLE sync_run ADD COLUMN IF NOT EXISTS active_items INTEGER;  -- requests are insert-only now

-- ---------------------------------------------------------------------------
-- Everyday views
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Derived objects (views + functions) are dropped and recreated on every init,
-- so upgrading from any older/newer version works even when a view's columns
-- changed (CREATE OR REPLACE VIEW can't drop or reorder columns). Tables above
-- are never dropped. All of this (down to the final COMMIT at the end of the file) runs in one transaction.
-- NOTE: CASCADE also drops any view someone else built on top of these.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS dash_allocations, dash_shipment_lines, dash_shipments, dash_requests, dash_supplies, dash_items,
                    v_review_queue, v_expiring, v_kpis, v_inventory CASCADE;
DROP FUNCTION IF EXISTS create_shipment(TEXT, JSONB, JSONB, BOOLEAN) CASCADE;
DROP FUNCTION IF EXISTS update_shipment(TEXT, JSONB) CASCADE;
DROP FUNCTION IF EXISTS set_shipment_lines(TEXT, JSONB) CASCADE;
DROP FUNCTION IF EXISTS set_shipment_line_flags(BIGINT, BOOLEAN, BOOLEAN) CASCADE;
DROP FUNCTION IF EXISTS b4p_stage_rank(TEXT) CASCADE;
DROP FUNCTION IF EXISTS b4p_line_fields(JSONB) CASCADE;
DROP FUNCTION IF EXISTS b4p_check_allocation(BIGINT, TEXT, INT) CASCADE;
DROP FUNCTION IF EXISTS b4p_shipment_header(JSONB) CASCADE;
DROP FUNCTION IF EXISTS b4p_ship_stock(TEXT) CASCADE;
DROP FUNCTION IF EXISTS set_item_quantity(TEXT, INT) CASCADE;
DROP FUNCTION IF EXISTS set_request_status(TEXT, TEXT) CASCADE;
DROP FUNCTION IF EXISTS create_request(TEXT, TEXT, JSONB) CASCADE;
DROP FUNCTION IF EXISTS create_request(TEXT, TEXT, JSONB, TEXT, TIMESTAMPTZ) CASCADE;
DROP FUNCTION IF EXISTS b4p_status(TEXT) CASCADE;
DROP FUNCTION IF EXISTS b4p_iso_utc(TIMESTAMPTZ) CASCADE;
DROP FUNCTION IF EXISTS b4p_has_phi(TEXT[]) CASCADE;
DROP FUNCTION IF EXISTS b4p_raw_has_phi(JSONB) CASCADE;
DROP FUNCTION IF EXISTS redact_phi(TEXT) CASCADE;
DROP FUNCTION IF EXISTS b4p_redact_raw(TEXT) CASCADE;
DROP FUNCTION IF EXISTS b4p_fold(TEXT) CASCADE;
DROP FUNCTION IF EXISTS b4p_patient_name_pattern() CASCADE;
DROP FUNCTION IF EXISTS b4p_patient_surname_pattern() CASCADE;
DROP FUNCTION IF EXISTS b4p_dob_pattern() CASCADE;
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
    SELECT '\y(?:[Pp][Tt]\y\.?|[Pp][Aa][Tt][Ii][Ee][Nn][Tt]\y)(?:\s*(?:[Ii][Dd]\y|#|[Nn][Oo]\y\.?))?[\s:#,.]*(?![0-9]{1,4}\s*(?:[Oo][Zz]|[Mm][Ll]|[Mm][Gg]|[Mm][Cc][Gg]|[Gg]|[Kk][Gg]|[Ll][Bb]|[Ll][Bb][Ss]|[Cc][Cc]|[Ll]|[Cc][Tt]|[Pp][Kk]|[Pp][Aa][Cc][Kk]|[Pp][Cc][Ss]|[Pp][Cc]|[Ii][Nn]|[Cc][Mm]|[Mm][Mm]|[Ff][Rr]|[Gg][Aa]|[Gg][Aa][Uu][Gg][Ee]|[Ii][Nn][Cc][Hh]|[Ii][Nn][Cc][Hh][Ee][Ss]|[Cc][Oo][Uu][Nn][Tt]|[Ee][Aa]|[Ee][Aa][Cc][Hh])\y)(?:(?!ID\y)[A-Z]{1,3}\y(?:\.[A-Z]{1,3}\y)*\.?(?![\''\u2019-]|\.?\s+[A-Z][a-z])|[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*)(?:[\s,]+(?:(?!ID\y)[A-Z]{1,3}\y(?:\.[A-Z]{1,3}\y)*\.?(?![\''\u2019-]|\.?\s+[A-Z][a-z])|[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*)){0,3}'
$f$;

-- Research-subject / specimen ID on trial sample tubes, e.g. "A00-000"
-- (letter, 2 digits, dash, 3 digits; case and spaces around the dash don't matter). Mirrors SUBJECT_ID_RE in normalize.py.
CREATE OR REPLACE FUNCTION b4p_subject_id_pattern() RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT '\y[A-Za-z][0-9]{2}\s*-\s*[0-9]{3}\y'
$f$;

-- A patient's name after "Patient:" / "Pt. Name:" (any case): up to three words, or
-- "Last, First". Mirrors PATIENT_NAME_PATTERN in normalize.py (tests compare them).
CREATE OR REPLACE FUNCTION b4p_patient_name_pattern() RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT '(?i)(?<![0-9])(?<![0-9]\s)\y(?:pt\y\.?|patient\y)(?:\s+name)?\s*(?::\s*(?!id\y)\S+(?:\s+\S+){0,2}|-\s*(?!(?:(?:id|name|return|starter|slipper|use|used|monitoring|monitor|disposable|reusable|single|sterile|belonging|lift|lifter|lifting|transfer|care|controlled|warming|warmer|gown|bed|safety|restraint|positioning|positioner|wedge|education|comfort|handling|ready|preoperative|preparation|specimen|sample|identification|information|wristband|armband|bracelet|band|wipe|isolation|support|cart|chair|hygiene|bathing|bath|skin|cleanser|slide|sheet|sling|aid|kit|supply|supplies|access|contact|cooling|heating|side|port|cable|circuit|interface|sitter|cuff|line|hose|tubing|mask|wound|dressing|glove|drape|cover|pillow|blanket|bag|cap|label|chart|record|data|room|left|right|instruction|tray|scale|thermometer|electrode|hoist|board|belt|shoe|sock|brief|diaper|underpad|pad|urinal|bedpan|commode|walker|wheelchair|stretcher|mattress|towel|washcloth|cloth|wash|hand|basin|item|product|set|pack|call|cord|alarm|garment|scrub|cushion|table|folder|transport|oxygen|suction|canister|gait|fall|mat|protection|turn|assist|valuable|pressure|simulator|gauze|needle|syringe|bandage|tape|hearing|hair|bib|temperature|probe|sensor|lead|system|pump|analgesia|catheter|bottle|container|cup|pitcher|tube|valve|filter|adapter|connector|clip|strap|harness|vest|garment|sleeve|stocking|boot|splint|brace|collar|rail|pole|stand|lamp|light|camera|phone|remote|control|button|switch|screen|display|software|program|plan|form|sticker|stamp|marker|pen|pencil|paper|envelope|file|binder|stool|charting|holder|privacy|weighing|compression|positioning|prep|device|lateral|resuscitator|vest|skin|safety|satisfaction|survey|bill|rights|portal|identification|armband|hospital|info|admission|shower|nasal|dental|dignity|swab|vomit|sharp|disposal|handbook|wt|spirometer)(?:e?s)?|id)\y)\S+(?:\s+\S+){0,2})'
$f$;

-- A name straight after Patient / Pt. with no colon ("Patient Johnson", "pt. smith"): one or two
-- words that are not ordinary supply words, followed by the end of the text, a comma, DOB/MRN/ID
-- or a number ("Patient Gait Belt" is a product).
-- Mirrors PATIENT_SURNAME_PATTERN in normalize.py.
CREATE OR REPLACE FUNCTION b4p_patient_surname_pattern() RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT '(?<![0-9])(?<![0-9]\s)\y(?:[Pp][Aa][Tt][Ii][Ee][Nn][Tt]\y|(?:Pt|PT)\y\.?|pt\y\.)\s+(?:(?:(?:[Dd][Ee]|[Ll][Aa]|[Dd][Ee][Ll]|[Dd][Ee][Ll][Ll][Aa]|[Vv][Aa][Nn]|[Dd][Ee][Rr]|[Dd][Ee][Nn]|[Vv][Oo][Nn]|[Dd][Aa]|[Dd][Ii]|[Ll][Ee]|[Dd][Uu]|[Tt][Ee][Rr]|[Aa][Ll]|[Ee][Ll]|[Bb][Ii][Nn]|[Ii][Bb][Nn])\s+){0,2}(?!(?:[Ii][Dd]|[Nn][Aa][Mm][Ee]|[Rr][Ee][Tt][Uu][Rr][Nn]|[Ss][Tt][Aa][Rr][Tt][Ee][Rr]|[Ss][Ll][Ii][Pp][Pp][Ee][Rr]|[Uu][Ss][Ee]|[Uu][Ss][Ee][Dd]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr][Ii][Nn][Gg]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Bb][Ll][Ee]|[Rr][Ee][Uu][Ss][Aa][Bb][Ll][Ee]|[Ss][Ii][Nn][Gg][Ll][Ee]|[Ss][Tt][Ee][Rr][Ii][Ll][Ee]|[Bb][Ee][Ll][Oo][Nn][Gg][Ii][Nn][Gg]|[Ll][Ii][Ff][Tt]|[Ll][Ii][Ff][Tt][Ee][Rr]|[Ll][Ii][Ff][Tt][Ii][Nn][Gg]|[Tt][Rr][Aa][Nn][Ss][Ff][Ee][Rr]|[Cc][Aa][Rr][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll][Ll][Ee][Dd]|[Ww][Aa][Rr][Mm][Ii][Nn][Gg]|[Ww][Aa][Rr][Mm][Ee][Rr]|[Gg][Oo][Ww][Nn]|[Bb][Ee][Dd]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Rr][Ee][Ss][Tt][Rr][Aa][Ii][Nn][Tt]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ee][Rr]|[Ww][Ee][Dd][Gg][Ee]|[Ee][Dd][Uu][Cc][Aa][Tt][Ii][Oo][Nn]|[Cc][Oo][Mm][Ff][Oo][Rr][Tt]|[Hh][Aa][Nn][Dd][Ll][Ii][Nn][Gg]|[Rr][Ee][Aa][Dd][Yy]|[Pp][Rr][Ee][Oo][Pp][Ee][Rr][Aa][Tt][Ii][Vv][Ee]|[Pp][Rr][Ee][Pp][Aa][Rr][Aa][Tt][Ii][Oo][Nn]|[Ss][Pp][Ee][Cc][Ii][Mm][Ee][Nn]|[Ss][Aa][Mm][Pp][Ll][Ee]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Ii][Nn][Ff][Oo][Rr][Mm][Aa][Tt][Ii][Oo][Nn]|[Ww][Rr][Ii][Ss][Tt][Bb][Aa][Nn][Dd]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Bb][Rr][Aa][Cc][Ee][Ll][Ee][Tt]|[Bb][Aa][Nn][Dd]|[Ww][Ii][Pp][Ee]|[Ii][Ss][Oo][Ll][Aa][Tt][Ii][Oo][Nn]|[Ss][Uu][Pp][Pp][Oo][Rr][Tt]|[Cc][Aa][Rr][Tt]|[Cc][Hh][Aa][Ii][Rr]|[Hh][Yy][Gg][Ii][Ee][Nn][Ee]|[Bb][Aa][Tt][Hh][Ii][Nn][Gg]|[Bb][Aa][Tt][Hh]|[Ss][Kk][Ii][Nn]|[Cc][Ll][Ee][Aa][Nn][Ss][Ee][Rr]|[Ss][Ll][Ii][Dd][Ee]|[Ss][Hh][Ee][Ee][Tt]|[Ss][Ll][Ii][Nn][Gg]|[Aa][Ii][Dd]|[Kk][Ii][Tt]|[Ss][Uu][Pp][Pp][Ll][Yy]|[Ss][Uu][Pp][Pp][Ll][Ii][Ee][Ss]|[Aa][Cc][Cc][Ee][Ss][Ss]|[Cc][Oo][Nn][Tt][Aa][Cc][Tt]|[Cc][Oo][Oo][Ll][Ii][Nn][Gg]|[Hh][Ee][Aa][Tt][Ii][Nn][Gg]|[Ss][Ii][Dd][Ee]|[Pp][Oo][Rr][Tt]|[Cc][Aa][Bb][Ll][Ee]|[Cc][Ii][Rr][Cc][Uu][Ii][Tt]|[Ii][Nn][Tt][Ee][Rr][Ff][Aa][Cc][Ee]|[Ss][Ii][Tt][Tt][Ee][Rr]|[Cc][Uu][Ff][Ff]|[Ll][Ii][Nn][Ee]|[Hh][Oo][Ss][Ee]|[Tt][Uu][Bb][Ii][Nn][Gg]|[Mm][Aa][Ss][Kk]|[Ww][Oo][Uu][Nn][Dd]|[Dd][Rr][Ee][Ss][Ss][Ii][Nn][Gg]|[Gg][Ll][Oo][Vv][Ee]|[Dd][Rr][Aa][Pp][Ee]|[Cc][Oo][Vv][Ee][Rr]|[Pp][Ii][Ll][Ll][Oo][Ww]|[Bb][Ll][Aa][Nn][Kk][Ee][Tt]|[Bb][Aa][Gg]|[Cc][Aa][Pp]|[Ll][Aa][Bb][Ee][Ll]|[Cc][Hh][Aa][Rr][Tt]|[Rr][Ee][Cc][Oo][Rr][Dd]|[Dd][Aa][Tt][Aa]|[Rr][Oo][Oo][Mm]|[Ll][Ee][Ff][Tt]|[Rr][Ii][Gg][Hh][Tt]|[Ii][Nn][Ss][Tt][Rr][Uu][Cc][Tt][Ii][Oo][Nn]|[Tt][Rr][Aa][Yy]|[Ss][Cc][Aa][Ll][Ee]|[Tt][Hh][Ee][Rr][Mm][Oo][Mm][Ee][Tt][Ee][Rr]|[Ee][Ll][Ee][Cc][Tt][Rr][Oo][Dd][Ee]|[Hh][Oo][Ii][Ss][Tt]|[Bb][Oo][Aa][Rr][Dd]|[Bb][Ee][Ll][Tt]|[Ss][Hh][Oo][Ee]|[Ss][Oo][Cc][Kk]|[Bb][Rr][Ii][Ee][Ff]|[Dd][Ii][Aa][Pp][Ee][Rr]|[Uu][Nn][Dd][Ee][Rr][Pp][Aa][Dd]|[Pp][Aa][Dd]|[Uu][Rr][Ii][Nn][Aa][Ll]|[Bb][Ee][Dd][Pp][Aa][Nn]|[Cc][Oo][Mm][Mm][Oo][Dd][Ee]|[Ww][Aa][Ll][Kk][Ee][Rr]|[Ww][Hh][Ee][Ee][Ll][Cc][Hh][Aa][Ii][Rr]|[Ss][Tt][Rr][Ee][Tt][Cc][Hh][Ee][Rr]|[Mm][Aa][Tt][Tt][Rr][Ee][Ss][Ss]|[Tt][Oo][Ww][Ee][Ll]|[Ww][Aa][Ss][Hh][Cc][Ll][Oo][Tt][Hh]|[Cc][Ll][Oo][Tt][Hh]|[Ww][Aa][Ss][Hh]|[Hh][Aa][Nn][Dd]|[Bb][Aa][Ss][Ii][Nn]|[Ii][Tt][Ee][Mm]|[Pp][Rr][Oo][Dd][Uu][Cc][Tt]|[Ss][Ee][Tt]|[Pp][Aa][Cc][Kk]|[Cc][Aa][Ll][Ll]|[Cc][Oo][Rr][Dd]|[Aa][Ll][Aa][Rr][Mm]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Cc][Rr][Uu][Bb]|[Cc][Uu][Ss][Hh][Ii][Oo][Nn]|[Tt][Aa][Bb][Ll][Ee]|[Ff][Oo][Ll][Dd][Ee][Rr]|[Tt][Rr][Aa][Nn][Ss][Pp][Oo][Rr][Tt]|[Oo][Xx][Yy][Gg][Ee][Nn]|[Ss][Uu][Cc][Tt][Ii][Oo][Nn]|[Cc][Aa][Nn][Ii][Ss][Tt][Ee][Rr]|[Gg][Aa][Ii][Tt]|[Ff][Aa][Ll][Ll]|[Mm][Aa][Tt]|[Pp][Rr][Oo][Tt][Ee][Cc][Tt][Ii][Oo][Nn]|[Tt][Uu][Rr][Nn]|[Aa][Ss][Ss][Ii][Ss][Tt]|[Vv][Aa][Ll][Uu][Aa][Bb][Ll][Ee]|[Pp][Rr][Ee][Ss][Ss][Uu][Rr][Ee]|[Ss][Ii][Mm][Uu][Ll][Aa][Tt][Oo][Rr]|[Gg][Aa][Uu][Zz][Ee]|[Nn][Ee][Ee][Dd][Ll][Ee]|[Ss][Yy][Rr][Ii][Nn][Gg][Ee]|[Bb][Aa][Nn][Dd][Aa][Gg][Ee]|[Tt][Aa][Pp][Ee]|[Hh][Ee][Aa][Rr][Ii][Nn][Gg]|[Hh][Aa][Ii][Rr]|[Bb][Ii][Bb]|[Tt][Ee][Mm][Pp][Ee][Rr][Aa][Tt][Uu][Rr][Ee]|[Pp][Rr][Oo][Bb][Ee]|[Ss][Ee][Nn][Ss][Oo][Rr]|[Ll][Ee][Aa][Dd]|[Ss][Yy][Ss][Tt][Ee][Mm]|[Pp][Uu][Mm][Pp]|[Aa][Nn][Aa][Ll][Gg][Ee][Ss][Ii][Aa]|[Cc][Aa][Tt][Hh][Ee][Tt][Ee][Rr]|[Bb][Oo][Tt][Tt][Ll][Ee]|[Cc][Oo][Nn][Tt][Aa][Ii][Nn][Ee][Rr]|[Cc][Uu][Pp]|[Pp][Ii][Tt][Cc][Hh][Ee][Rr]|[Tt][Uu][Bb][Ee]|[Vv][Aa][Ll][Vv][Ee]|[Ff][Ii][Ll][Tt][Ee][Rr]|[Aa][Dd][Aa][Pp][Tt][Ee][Rr]|[Cc][Oo][Nn][Nn][Ee][Cc][Tt][Oo][Rr]|[Cc][Ll][Ii][Pp]|[Ss][Tt][Rr][Aa][Pp]|[Hh][Aa][Rr][Nn][Ee][Ss][Ss]|[Vv][Ee][Ss][Tt]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Ll][Ee][Ee][Vv][Ee]|[Ss][Tt][Oo][Cc][Kk][Ii][Nn][Gg]|[Bb][Oo][Oo][Tt]|[Ss][Pp][Ll][Ii][Nn][Tt]|[Bb][Rr][Aa][Cc][Ee]|[Cc][Oo][Ll][Ll][Aa][Rr]|[Rr][Aa][Ii][Ll]|[Pp][Oo][Ll][Ee]|[Ss][Tt][Aa][Nn][Dd]|[Ll][Aa][Mm][Pp]|[Ll][Ii][Gg][Hh][Tt]|[Cc][Aa][Mm][Ee][Rr][Aa]|[Pp][Hh][Oo][Nn][Ee]|[Rr][Ee][Mm][Oo][Tt][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll]|[Bb][Uu][Tt][Tt][Oo][Nn]|[Ss][Ww][Ii][Tt][Cc][Hh]|[Ss][Cc][Rr][Ee][Ee][Nn]|[Dd][Ii][Ss][Pp][Ll][Aa][Yy]|[Ss][Oo][Ff][Tt][Ww][Aa][Rr][Ee]|[Pp][Rr][Oo][Gg][Rr][Aa][Mm]|[Pp][Ll][Aa][Nn]|[Ff][Oo][Rr][Mm]|[Ss][Tt][Ii][Cc][Kk][Ee][Rr]|[Ss][Tt][Aa][Mm][Pp]|[Mm][Aa][Rr][Kk][Ee][Rr]|[Pp][Ee][Nn]|[Pp][Ee][Nn][Cc][Ii][Ll]|[Pp][Aa][Pp][Ee][Rr]|[Ee][Nn][Vv][Ee][Ll][Oo][Pp][Ee]|[Ff][Ii][Ll][Ee]|[Bb][Ii][Nn][Dd][Ee][Rr]|[Ss][Tt][Oo][Oo][Ll]|[Cc][Hh][Aa][Rr][Tt][Ii][Nn][Gg]|[Hh][Oo][Ll][Dd][Ee][Rr]|[Pp][Rr][Ii][Vv][Aa][Cc][Yy]|[Ww][Ee][Ii][Gg][Hh][Ii][Nn][Gg]|[Cc][Oo][Mm][Pp][Rr][Ee][Ss][Ss][Ii][Oo][Nn]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Rr][Ee][Pp]|[Dd][Ee][Vv][Ii][Cc][Ee]|[Ll][Aa][Tt][Ee][Rr][Aa][Ll]|[Rr][Ee][Ss][Uu][Ss][Cc][Ii][Tt][Aa][Tt][Oo][Rr]|[Vv][Ee][Ss][Tt]|[Ss][Kk][Ii][Nn]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Ss][Aa][Tt][Ii][Ss][Ff][Aa][Cc][Tt][Ii][Oo][Nn]|[Ss][Uu][Rr][Vv][Ee][Yy]|[Bb][Ii][Ll][Ll]|[Rr][Ii][Gg][Hh][Tt][Ss]|[Pp][Oo][Rr][Tt][Aa][Ll]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Hh][Oo][Ss][Pp][Ii][Tt][Aa][Ll]|[Ii][Nn][Ff][Oo]|[Aa][Dd][Mm][Ii][Ss][Ss][Ii][Oo][Nn]|[Ss][Hh][Oo][Ww][Ee][Rr]|[Nn][Aa][Ss][Aa][Ll]|[Dd][Ee][Nn][Tt][Aa][Ll]|[Dd][Ii][Gg][Nn][Ii][Tt][Yy]|[Ss][Ww][Aa][Bb]|[Vv][Oo][Mm][Ii][Tt]|[Ss][Hh][Aa][Rr][Pp]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Ll]|[Hh][Aa][Nn][Dd][Bb][Oo][Oo][Kk]|[Ww][Tt]|[Ss][Pp][Ii][Rr][Oo][Mm][Ee][Tt][Ee][Rr])(?:[Ee]?[Ss])?\y)[A-ZÀ-ÞΑ-ΩА-Я぀-ヿ㐀-鿿가-힯][A-Za-zÀ-ɏͰ-ϿЀ-ӿḀ-ỿ぀-ヿ㐀-鿿가-힯''\u2019/.-]*(?:\s+(?:(?:[Dd][Ee]|[Ll][Aa]|[Dd][Ee][Ll]|[Dd][Ee][Ll][Ll][Aa]|[Vv][Aa][Nn]|[Dd][Ee][Rr]|[Dd][Ee][Nn]|[Vv][Oo][Nn]|[Dd][Aa]|[Dd][Ii]|[Ll][Ee]|[Dd][Uu]|[Tt][Ee][Rr]|[Aa][Ll]|[Ee][Ll]|[Bb][Ii][Nn]|[Ii][Bb][Nn])\s+){0,2}(?!(?:[Ii][Dd]|[Nn][Aa][Mm][Ee]|[Rr][Ee][Tt][Uu][Rr][Nn]|[Ss][Tt][Aa][Rr][Tt][Ee][Rr]|[Ss][Ll][Ii][Pp][Pp][Ee][Rr]|[Uu][Ss][Ee]|[Uu][Ss][Ee][Dd]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr][Ii][Nn][Gg]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Bb][Ll][Ee]|[Rr][Ee][Uu][Ss][Aa][Bb][Ll][Ee]|[Ss][Ii][Nn][Gg][Ll][Ee]|[Ss][Tt][Ee][Rr][Ii][Ll][Ee]|[Bb][Ee][Ll][Oo][Nn][Gg][Ii][Nn][Gg]|[Ll][Ii][Ff][Tt]|[Ll][Ii][Ff][Tt][Ee][Rr]|[Ll][Ii][Ff][Tt][Ii][Nn][Gg]|[Tt][Rr][Aa][Nn][Ss][Ff][Ee][Rr]|[Cc][Aa][Rr][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll][Ll][Ee][Dd]|[Ww][Aa][Rr][Mm][Ii][Nn][Gg]|[Ww][Aa][Rr][Mm][Ee][Rr]|[Gg][Oo][Ww][Nn]|[Bb][Ee][Dd]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Rr][Ee][Ss][Tt][Rr][Aa][Ii][Nn][Tt]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ee][Rr]|[Ww][Ee][Dd][Gg][Ee]|[Ee][Dd][Uu][Cc][Aa][Tt][Ii][Oo][Nn]|[Cc][Oo][Mm][Ff][Oo][Rr][Tt]|[Hh][Aa][Nn][Dd][Ll][Ii][Nn][Gg]|[Rr][Ee][Aa][Dd][Yy]|[Pp][Rr][Ee][Oo][Pp][Ee][Rr][Aa][Tt][Ii][Vv][Ee]|[Pp][Rr][Ee][Pp][Aa][Rr][Aa][Tt][Ii][Oo][Nn]|[Ss][Pp][Ee][Cc][Ii][Mm][Ee][Nn]|[Ss][Aa][Mm][Pp][Ll][Ee]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Ii][Nn][Ff][Oo][Rr][Mm][Aa][Tt][Ii][Oo][Nn]|[Ww][Rr][Ii][Ss][Tt][Bb][Aa][Nn][Dd]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Bb][Rr][Aa][Cc][Ee][Ll][Ee][Tt]|[Bb][Aa][Nn][Dd]|[Ww][Ii][Pp][Ee]|[Ii][Ss][Oo][Ll][Aa][Tt][Ii][Oo][Nn]|[Ss][Uu][Pp][Pp][Oo][Rr][Tt]|[Cc][Aa][Rr][Tt]|[Cc][Hh][Aa][Ii][Rr]|[Hh][Yy][Gg][Ii][Ee][Nn][Ee]|[Bb][Aa][Tt][Hh][Ii][Nn][Gg]|[Bb][Aa][Tt][Hh]|[Ss][Kk][Ii][Nn]|[Cc][Ll][Ee][Aa][Nn][Ss][Ee][Rr]|[Ss][Ll][Ii][Dd][Ee]|[Ss][Hh][Ee][Ee][Tt]|[Ss][Ll][Ii][Nn][Gg]|[Aa][Ii][Dd]|[Kk][Ii][Tt]|[Ss][Uu][Pp][Pp][Ll][Yy]|[Ss][Uu][Pp][Pp][Ll][Ii][Ee][Ss]|[Aa][Cc][Cc][Ee][Ss][Ss]|[Cc][Oo][Nn][Tt][Aa][Cc][Tt]|[Cc][Oo][Oo][Ll][Ii][Nn][Gg]|[Hh][Ee][Aa][Tt][Ii][Nn][Gg]|[Ss][Ii][Dd][Ee]|[Pp][Oo][Rr][Tt]|[Cc][Aa][Bb][Ll][Ee]|[Cc][Ii][Rr][Cc][Uu][Ii][Tt]|[Ii][Nn][Tt][Ee][Rr][Ff][Aa][Cc][Ee]|[Ss][Ii][Tt][Tt][Ee][Rr]|[Cc][Uu][Ff][Ff]|[Ll][Ii][Nn][Ee]|[Hh][Oo][Ss][Ee]|[Tt][Uu][Bb][Ii][Nn][Gg]|[Mm][Aa][Ss][Kk]|[Ww][Oo][Uu][Nn][Dd]|[Dd][Rr][Ee][Ss][Ss][Ii][Nn][Gg]|[Gg][Ll][Oo][Vv][Ee]|[Dd][Rr][Aa][Pp][Ee]|[Cc][Oo][Vv][Ee][Rr]|[Pp][Ii][Ll][Ll][Oo][Ww]|[Bb][Ll][Aa][Nn][Kk][Ee][Tt]|[Bb][Aa][Gg]|[Cc][Aa][Pp]|[Ll][Aa][Bb][Ee][Ll]|[Cc][Hh][Aa][Rr][Tt]|[Rr][Ee][Cc][Oo][Rr][Dd]|[Dd][Aa][Tt][Aa]|[Rr][Oo][Oo][Mm]|[Ll][Ee][Ff][Tt]|[Rr][Ii][Gg][Hh][Tt]|[Ii][Nn][Ss][Tt][Rr][Uu][Cc][Tt][Ii][Oo][Nn]|[Tt][Rr][Aa][Yy]|[Ss][Cc][Aa][Ll][Ee]|[Tt][Hh][Ee][Rr][Mm][Oo][Mm][Ee][Tt][Ee][Rr]|[Ee][Ll][Ee][Cc][Tt][Rr][Oo][Dd][Ee]|[Hh][Oo][Ii][Ss][Tt]|[Bb][Oo][Aa][Rr][Dd]|[Bb][Ee][Ll][Tt]|[Ss][Hh][Oo][Ee]|[Ss][Oo][Cc][Kk]|[Bb][Rr][Ii][Ee][Ff]|[Dd][Ii][Aa][Pp][Ee][Rr]|[Uu][Nn][Dd][Ee][Rr][Pp][Aa][Dd]|[Pp][Aa][Dd]|[Uu][Rr][Ii][Nn][Aa][Ll]|[Bb][Ee][Dd][Pp][Aa][Nn]|[Cc][Oo][Mm][Mm][Oo][Dd][Ee]|[Ww][Aa][Ll][Kk][Ee][Rr]|[Ww][Hh][Ee][Ee][Ll][Cc][Hh][Aa][Ii][Rr]|[Ss][Tt][Rr][Ee][Tt][Cc][Hh][Ee][Rr]|[Mm][Aa][Tt][Tt][Rr][Ee][Ss][Ss]|[Tt][Oo][Ww][Ee][Ll]|[Ww][Aa][Ss][Hh][Cc][Ll][Oo][Tt][Hh]|[Cc][Ll][Oo][Tt][Hh]|[Ww][Aa][Ss][Hh]|[Hh][Aa][Nn][Dd]|[Bb][Aa][Ss][Ii][Nn]|[Ii][Tt][Ee][Mm]|[Pp][Rr][Oo][Dd][Uu][Cc][Tt]|[Ss][Ee][Tt]|[Pp][Aa][Cc][Kk]|[Cc][Aa][Ll][Ll]|[Cc][Oo][Rr][Dd]|[Aa][Ll][Aa][Rr][Mm]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Cc][Rr][Uu][Bb]|[Cc][Uu][Ss][Hh][Ii][Oo][Nn]|[Tt][Aa][Bb][Ll][Ee]|[Ff][Oo][Ll][Dd][Ee][Rr]|[Tt][Rr][Aa][Nn][Ss][Pp][Oo][Rr][Tt]|[Oo][Xx][Yy][Gg][Ee][Nn]|[Ss][Uu][Cc][Tt][Ii][Oo][Nn]|[Cc][Aa][Nn][Ii][Ss][Tt][Ee][Rr]|[Gg][Aa][Ii][Tt]|[Ff][Aa][Ll][Ll]|[Mm][Aa][Tt]|[Pp][Rr][Oo][Tt][Ee][Cc][Tt][Ii][Oo][Nn]|[Tt][Uu][Rr][Nn]|[Aa][Ss][Ss][Ii][Ss][Tt]|[Vv][Aa][Ll][Uu][Aa][Bb][Ll][Ee]|[Pp][Rr][Ee][Ss][Ss][Uu][Rr][Ee]|[Ss][Ii][Mm][Uu][Ll][Aa][Tt][Oo][Rr]|[Gg][Aa][Uu][Zz][Ee]|[Nn][Ee][Ee][Dd][Ll][Ee]|[Ss][Yy][Rr][Ii][Nn][Gg][Ee]|[Bb][Aa][Nn][Dd][Aa][Gg][Ee]|[Tt][Aa][Pp][Ee]|[Hh][Ee][Aa][Rr][Ii][Nn][Gg]|[Hh][Aa][Ii][Rr]|[Bb][Ii][Bb]|[Tt][Ee][Mm][Pp][Ee][Rr][Aa][Tt][Uu][Rr][Ee]|[Pp][Rr][Oo][Bb][Ee]|[Ss][Ee][Nn][Ss][Oo][Rr]|[Ll][Ee][Aa][Dd]|[Ss][Yy][Ss][Tt][Ee][Mm]|[Pp][Uu][Mm][Pp]|[Aa][Nn][Aa][Ll][Gg][Ee][Ss][Ii][Aa]|[Cc][Aa][Tt][Hh][Ee][Tt][Ee][Rr]|[Bb][Oo][Tt][Tt][Ll][Ee]|[Cc][Oo][Nn][Tt][Aa][Ii][Nn][Ee][Rr]|[Cc][Uu][Pp]|[Pp][Ii][Tt][Cc][Hh][Ee][Rr]|[Tt][Uu][Bb][Ee]|[Vv][Aa][Ll][Vv][Ee]|[Ff][Ii][Ll][Tt][Ee][Rr]|[Aa][Dd][Aa][Pp][Tt][Ee][Rr]|[Cc][Oo][Nn][Nn][Ee][Cc][Tt][Oo][Rr]|[Cc][Ll][Ii][Pp]|[Ss][Tt][Rr][Aa][Pp]|[Hh][Aa][Rr][Nn][Ee][Ss][Ss]|[Vv][Ee][Ss][Tt]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Ll][Ee][Ee][Vv][Ee]|[Ss][Tt][Oo][Cc][Kk][Ii][Nn][Gg]|[Bb][Oo][Oo][Tt]|[Ss][Pp][Ll][Ii][Nn][Tt]|[Bb][Rr][Aa][Cc][Ee]|[Cc][Oo][Ll][Ll][Aa][Rr]|[Rr][Aa][Ii][Ll]|[Pp][Oo][Ll][Ee]|[Ss][Tt][Aa][Nn][Dd]|[Ll][Aa][Mm][Pp]|[Ll][Ii][Gg][Hh][Tt]|[Cc][Aa][Mm][Ee][Rr][Aa]|[Pp][Hh][Oo][Nn][Ee]|[Rr][Ee][Mm][Oo][Tt][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll]|[Bb][Uu][Tt][Tt][Oo][Nn]|[Ss][Ww][Ii][Tt][Cc][Hh]|[Ss][Cc][Rr][Ee][Ee][Nn]|[Dd][Ii][Ss][Pp][Ll][Aa][Yy]|[Ss][Oo][Ff][Tt][Ww][Aa][Rr][Ee]|[Pp][Rr][Oo][Gg][Rr][Aa][Mm]|[Pp][Ll][Aa][Nn]|[Ff][Oo][Rr][Mm]|[Ss][Tt][Ii][Cc][Kk][Ee][Rr]|[Ss][Tt][Aa][Mm][Pp]|[Mm][Aa][Rr][Kk][Ee][Rr]|[Pp][Ee][Nn]|[Pp][Ee][Nn][Cc][Ii][Ll]|[Pp][Aa][Pp][Ee][Rr]|[Ee][Nn][Vv][Ee][Ll][Oo][Pp][Ee]|[Ff][Ii][Ll][Ee]|[Bb][Ii][Nn][Dd][Ee][Rr]|[Ss][Tt][Oo][Oo][Ll]|[Cc][Hh][Aa][Rr][Tt][Ii][Nn][Gg]|[Hh][Oo][Ll][Dd][Ee][Rr]|[Pp][Rr][Ii][Vv][Aa][Cc][Yy]|[Ww][Ee][Ii][Gg][Hh][Ii][Nn][Gg]|[Cc][Oo][Mm][Pp][Rr][Ee][Ss][Ss][Ii][Oo][Nn]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Rr][Ee][Pp]|[Dd][Ee][Vv][Ii][Cc][Ee]|[Ll][Aa][Tt][Ee][Rr][Aa][Ll]|[Rr][Ee][Ss][Uu][Ss][Cc][Ii][Tt][Aa][Tt][Oo][Rr]|[Vv][Ee][Ss][Tt]|[Ss][Kk][Ii][Nn]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Ss][Aa][Tt][Ii][Ss][Ff][Aa][Cc][Tt][Ii][Oo][Nn]|[Ss][Uu][Rr][Vv][Ee][Yy]|[Bb][Ii][Ll][Ll]|[Rr][Ii][Gg][Hh][Tt][Ss]|[Pp][Oo][Rr][Tt][Aa][Ll]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Hh][Oo][Ss][Pp][Ii][Tt][Aa][Ll]|[Ii][Nn][Ff][Oo]|[Aa][Dd][Mm][Ii][Ss][Ss][Ii][Oo][Nn]|[Ss][Hh][Oo][Ww][Ee][Rr]|[Nn][Aa][Ss][Aa][Ll]|[Dd][Ee][Nn][Tt][Aa][Ll]|[Dd][Ii][Gg][Nn][Ii][Tt][Yy]|[Ss][Ww][Aa][Bb]|[Vv][Oo][Mm][Ii][Tt]|[Ss][Hh][Aa][Rr][Pp]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Ll]|[Hh][Aa][Nn][Dd][Bb][Oo][Oo][Kk]|[Ww][Tt]|[Ss][Pp][Ii][Rr][Oo][Mm][Ee][Tt][Ee][Rr])(?:[Ee]?[Ss])?\y)[A-ZÀ-ÞΑ-ΩА-Я぀-ヿ㐀-鿿가-힯][A-Za-zÀ-ɏͰ-ϿЀ-ӿḀ-ỿ぀-ヿ㐀-鿿가-힯''\u2019/.-]*){0,2}|(?!(?:[Ii][Dd]|[Nn][Aa][Mm][Ee]|[Rr][Ee][Tt][Uu][Rr][Nn]|[Ss][Tt][Aa][Rr][Tt][Ee][Rr]|[Ss][Ll][Ii][Pp][Pp][Ee][Rr]|[Uu][Ss][Ee]|[Uu][Ss][Ee][Dd]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr][Ii][Nn][Gg]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Bb][Ll][Ee]|[Rr][Ee][Uu][Ss][Aa][Bb][Ll][Ee]|[Ss][Ii][Nn][Gg][Ll][Ee]|[Ss][Tt][Ee][Rr][Ii][Ll][Ee]|[Bb][Ee][Ll][Oo][Nn][Gg][Ii][Nn][Gg]|[Ll][Ii][Ff][Tt]|[Ll][Ii][Ff][Tt][Ee][Rr]|[Ll][Ii][Ff][Tt][Ii][Nn][Gg]|[Tt][Rr][Aa][Nn][Ss][Ff][Ee][Rr]|[Cc][Aa][Rr][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll][Ll][Ee][Dd]|[Ww][Aa][Rr][Mm][Ii][Nn][Gg]|[Ww][Aa][Rr][Mm][Ee][Rr]|[Gg][Oo][Ww][Nn]|[Bb][Ee][Dd]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Rr][Ee][Ss][Tt][Rr][Aa][Ii][Nn][Tt]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ee][Rr]|[Ww][Ee][Dd][Gg][Ee]|[Ee][Dd][Uu][Cc][Aa][Tt][Ii][Oo][Nn]|[Cc][Oo][Mm][Ff][Oo][Rr][Tt]|[Hh][Aa][Nn][Dd][Ll][Ii][Nn][Gg]|[Rr][Ee][Aa][Dd][Yy]|[Pp][Rr][Ee][Oo][Pp][Ee][Rr][Aa][Tt][Ii][Vv][Ee]|[Pp][Rr][Ee][Pp][Aa][Rr][Aa][Tt][Ii][Oo][Nn]|[Ss][Pp][Ee][Cc][Ii][Mm][Ee][Nn]|[Ss][Aa][Mm][Pp][Ll][Ee]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Ii][Nn][Ff][Oo][Rr][Mm][Aa][Tt][Ii][Oo][Nn]|[Ww][Rr][Ii][Ss][Tt][Bb][Aa][Nn][Dd]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Bb][Rr][Aa][Cc][Ee][Ll][Ee][Tt]|[Bb][Aa][Nn][Dd]|[Ww][Ii][Pp][Ee]|[Ii][Ss][Oo][Ll][Aa][Tt][Ii][Oo][Nn]|[Ss][Uu][Pp][Pp][Oo][Rr][Tt]|[Cc][Aa][Rr][Tt]|[Cc][Hh][Aa][Ii][Rr]|[Hh][Yy][Gg][Ii][Ee][Nn][Ee]|[Bb][Aa][Tt][Hh][Ii][Nn][Gg]|[Bb][Aa][Tt][Hh]|[Ss][Kk][Ii][Nn]|[Cc][Ll][Ee][Aa][Nn][Ss][Ee][Rr]|[Ss][Ll][Ii][Dd][Ee]|[Ss][Hh][Ee][Ee][Tt]|[Ss][Ll][Ii][Nn][Gg]|[Aa][Ii][Dd]|[Kk][Ii][Tt]|[Ss][Uu][Pp][Pp][Ll][Yy]|[Ss][Uu][Pp][Pp][Ll][Ii][Ee][Ss]|[Aa][Cc][Cc][Ee][Ss][Ss]|[Cc][Oo][Nn][Tt][Aa][Cc][Tt]|[Cc][Oo][Oo][Ll][Ii][Nn][Gg]|[Hh][Ee][Aa][Tt][Ii][Nn][Gg]|[Ss][Ii][Dd][Ee]|[Pp][Oo][Rr][Tt]|[Cc][Aa][Bb][Ll][Ee]|[Cc][Ii][Rr][Cc][Uu][Ii][Tt]|[Ii][Nn][Tt][Ee][Rr][Ff][Aa][Cc][Ee]|[Ss][Ii][Tt][Tt][Ee][Rr]|[Cc][Uu][Ff][Ff]|[Ll][Ii][Nn][Ee]|[Hh][Oo][Ss][Ee]|[Tt][Uu][Bb][Ii][Nn][Gg]|[Mm][Aa][Ss][Kk]|[Ww][Oo][Uu][Nn][Dd]|[Dd][Rr][Ee][Ss][Ss][Ii][Nn][Gg]|[Gg][Ll][Oo][Vv][Ee]|[Dd][Rr][Aa][Pp][Ee]|[Cc][Oo][Vv][Ee][Rr]|[Pp][Ii][Ll][Ll][Oo][Ww]|[Bb][Ll][Aa][Nn][Kk][Ee][Tt]|[Bb][Aa][Gg]|[Cc][Aa][Pp]|[Ll][Aa][Bb][Ee][Ll]|[Cc][Hh][Aa][Rr][Tt]|[Rr][Ee][Cc][Oo][Rr][Dd]|[Dd][Aa][Tt][Aa]|[Rr][Oo][Oo][Mm]|[Ll][Ee][Ff][Tt]|[Rr][Ii][Gg][Hh][Tt]|[Ii][Nn][Ss][Tt][Rr][Uu][Cc][Tt][Ii][Oo][Nn]|[Tt][Rr][Aa][Yy]|[Ss][Cc][Aa][Ll][Ee]|[Tt][Hh][Ee][Rr][Mm][Oo][Mm][Ee][Tt][Ee][Rr]|[Ee][Ll][Ee][Cc][Tt][Rr][Oo][Dd][Ee]|[Hh][Oo][Ii][Ss][Tt]|[Bb][Oo][Aa][Rr][Dd]|[Bb][Ee][Ll][Tt]|[Ss][Hh][Oo][Ee]|[Ss][Oo][Cc][Kk]|[Bb][Rr][Ii][Ee][Ff]|[Dd][Ii][Aa][Pp][Ee][Rr]|[Uu][Nn][Dd][Ee][Rr][Pp][Aa][Dd]|[Pp][Aa][Dd]|[Uu][Rr][Ii][Nn][Aa][Ll]|[Bb][Ee][Dd][Pp][Aa][Nn]|[Cc][Oo][Mm][Mm][Oo][Dd][Ee]|[Ww][Aa][Ll][Kk][Ee][Rr]|[Ww][Hh][Ee][Ee][Ll][Cc][Hh][Aa][Ii][Rr]|[Ss][Tt][Rr][Ee][Tt][Cc][Hh][Ee][Rr]|[Mm][Aa][Tt][Tt][Rr][Ee][Ss][Ss]|[Tt][Oo][Ww][Ee][Ll]|[Ww][Aa][Ss][Hh][Cc][Ll][Oo][Tt][Hh]|[Cc][Ll][Oo][Tt][Hh]|[Ww][Aa][Ss][Hh]|[Hh][Aa][Nn][Dd]|[Bb][Aa][Ss][Ii][Nn]|[Ii][Tt][Ee][Mm]|[Pp][Rr][Oo][Dd][Uu][Cc][Tt]|[Ss][Ee][Tt]|[Pp][Aa][Cc][Kk]|[Cc][Aa][Ll][Ll]|[Cc][Oo][Rr][Dd]|[Aa][Ll][Aa][Rr][Mm]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Cc][Rr][Uu][Bb]|[Cc][Uu][Ss][Hh][Ii][Oo][Nn]|[Tt][Aa][Bb][Ll][Ee]|[Ff][Oo][Ll][Dd][Ee][Rr]|[Tt][Rr][Aa][Nn][Ss][Pp][Oo][Rr][Tt]|[Oo][Xx][Yy][Gg][Ee][Nn]|[Ss][Uu][Cc][Tt][Ii][Oo][Nn]|[Cc][Aa][Nn][Ii][Ss][Tt][Ee][Rr]|[Gg][Aa][Ii][Tt]|[Ff][Aa][Ll][Ll]|[Mm][Aa][Tt]|[Pp][Rr][Oo][Tt][Ee][Cc][Tt][Ii][Oo][Nn]|[Tt][Uu][Rr][Nn]|[Aa][Ss][Ss][Ii][Ss][Tt]|[Vv][Aa][Ll][Uu][Aa][Bb][Ll][Ee]|[Pp][Rr][Ee][Ss][Ss][Uu][Rr][Ee]|[Ss][Ii][Mm][Uu][Ll][Aa][Tt][Oo][Rr]|[Gg][Aa][Uu][Zz][Ee]|[Nn][Ee][Ee][Dd][Ll][Ee]|[Ss][Yy][Rr][Ii][Nn][Gg][Ee]|[Bb][Aa][Nn][Dd][Aa][Gg][Ee]|[Tt][Aa][Pp][Ee]|[Hh][Ee][Aa][Rr][Ii][Nn][Gg]|[Hh][Aa][Ii][Rr]|[Bb][Ii][Bb]|[Tt][Ee][Mm][Pp][Ee][Rr][Aa][Tt][Uu][Rr][Ee]|[Pp][Rr][Oo][Bb][Ee]|[Ss][Ee][Nn][Ss][Oo][Rr]|[Ll][Ee][Aa][Dd]|[Ss][Yy][Ss][Tt][Ee][Mm]|[Pp][Uu][Mm][Pp]|[Aa][Nn][Aa][Ll][Gg][Ee][Ss][Ii][Aa]|[Cc][Aa][Tt][Hh][Ee][Tt][Ee][Rr]|[Bb][Oo][Tt][Tt][Ll][Ee]|[Cc][Oo][Nn][Tt][Aa][Ii][Nn][Ee][Rr]|[Cc][Uu][Pp]|[Pp][Ii][Tt][Cc][Hh][Ee][Rr]|[Tt][Uu][Bb][Ee]|[Vv][Aa][Ll][Vv][Ee]|[Ff][Ii][Ll][Tt][Ee][Rr]|[Aa][Dd][Aa][Pp][Tt][Ee][Rr]|[Cc][Oo][Nn][Nn][Ee][Cc][Tt][Oo][Rr]|[Cc][Ll][Ii][Pp]|[Ss][Tt][Rr][Aa][Pp]|[Hh][Aa][Rr][Nn][Ee][Ss][Ss]|[Vv][Ee][Ss][Tt]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Ll][Ee][Ee][Vv][Ee]|[Ss][Tt][Oo][Cc][Kk][Ii][Nn][Gg]|[Bb][Oo][Oo][Tt]|[Ss][Pp][Ll][Ii][Nn][Tt]|[Bb][Rr][Aa][Cc][Ee]|[Cc][Oo][Ll][Ll][Aa][Rr]|[Rr][Aa][Ii][Ll]|[Pp][Oo][Ll][Ee]|[Ss][Tt][Aa][Nn][Dd]|[Ll][Aa][Mm][Pp]|[Ll][Ii][Gg][Hh][Tt]|[Cc][Aa][Mm][Ee][Rr][Aa]|[Pp][Hh][Oo][Nn][Ee]|[Rr][Ee][Mm][Oo][Tt][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll]|[Bb][Uu][Tt][Tt][Oo][Nn]|[Ss][Ww][Ii][Tt][Cc][Hh]|[Ss][Cc][Rr][Ee][Ee][Nn]|[Dd][Ii][Ss][Pp][Ll][Aa][Yy]|[Ss][Oo][Ff][Tt][Ww][Aa][Rr][Ee]|[Pp][Rr][Oo][Gg][Rr][Aa][Mm]|[Pp][Ll][Aa][Nn]|[Ff][Oo][Rr][Mm]|[Ss][Tt][Ii][Cc][Kk][Ee][Rr]|[Ss][Tt][Aa][Mm][Pp]|[Mm][Aa][Rr][Kk][Ee][Rr]|[Pp][Ee][Nn]|[Pp][Ee][Nn][Cc][Ii][Ll]|[Pp][Aa][Pp][Ee][Rr]|[Ee][Nn][Vv][Ee][Ll][Oo][Pp][Ee]|[Ff][Ii][Ll][Ee]|[Bb][Ii][Nn][Dd][Ee][Rr]|[Ss][Tt][Oo][Oo][Ll]|[Cc][Hh][Aa][Rr][Tt][Ii][Nn][Gg]|[Hh][Oo][Ll][Dd][Ee][Rr]|[Pp][Rr][Ii][Vv][Aa][Cc][Yy]|[Ww][Ee][Ii][Gg][Hh][Ii][Nn][Gg]|[Cc][Oo][Mm][Pp][Rr][Ee][Ss][Ss][Ii][Oo][Nn]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Rr][Ee][Pp]|[Dd][Ee][Vv][Ii][Cc][Ee]|[Ll][Aa][Tt][Ee][Rr][Aa][Ll]|[Rr][Ee][Ss][Uu][Ss][Cc][Ii][Tt][Aa][Tt][Oo][Rr]|[Vv][Ee][Ss][Tt]|[Ss][Kk][Ii][Nn]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Ss][Aa][Tt][Ii][Ss][Ff][Aa][Cc][Tt][Ii][Oo][Nn]|[Ss][Uu][Rr][Vv][Ee][Yy]|[Bb][Ii][Ll][Ll]|[Rr][Ii][Gg][Hh][Tt][Ss]|[Pp][Oo][Rr][Tt][Aa][Ll]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Hh][Oo][Ss][Pp][Ii][Tt][Aa][Ll]|[Ii][Nn][Ff][Oo]|[Aa][Dd][Mm][Ii][Ss][Ss][Ii][Oo][Nn]|[Ss][Hh][Oo][Ww][Ee][Rr]|[Nn][Aa][Ss][Aa][Ll]|[Dd][Ee][Nn][Tt][Aa][Ll]|[Dd][Ii][Gg][Nn][Ii][Tt][Yy]|[Ss][Ww][Aa][Bb]|[Vv][Oo][Mm][Ii][Tt]|[Ss][Hh][Aa][Rr][Pp]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Ll]|[Hh][Aa][Nn][Dd][Bb][Oo][Oo][Kk]|[Ww][Tt]|[Ss][Pp][Ii][Rr][Oo][Mm][Ee][Tt][Ee][Rr])(?:[Ee]?[Ss])?\y)[A-Za-zÀ-ɏͰ-ϿЀ-ӿḀ-ỿ''\u2019-]+(?![A-Za-zÀ-ɏͰ-ϿЀ-ӿḀ-ỿ0-9])(?:\s+(?!(?:[Ii][Dd]|[Nn][Aa][Mm][Ee]|[Rr][Ee][Tt][Uu][Rr][Nn]|[Ss][Tt][Aa][Rr][Tt][Ee][Rr]|[Ss][Ll][Ii][Pp][Pp][Ee][Rr]|[Uu][Ss][Ee]|[Uu][Ss][Ee][Dd]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr][Ii][Nn][Gg]|[Mm][Oo][Nn][Ii][Tt][Oo][Rr]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Bb][Ll][Ee]|[Rr][Ee][Uu][Ss][Aa][Bb][Ll][Ee]|[Ss][Ii][Nn][Gg][Ll][Ee]|[Ss][Tt][Ee][Rr][Ii][Ll][Ee]|[Bb][Ee][Ll][Oo][Nn][Gg][Ii][Nn][Gg]|[Ll][Ii][Ff][Tt]|[Ll][Ii][Ff][Tt][Ee][Rr]|[Ll][Ii][Ff][Tt][Ii][Nn][Gg]|[Tt][Rr][Aa][Nn][Ss][Ff][Ee][Rr]|[Cc][Aa][Rr][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll][Ll][Ee][Dd]|[Ww][Aa][Rr][Mm][Ii][Nn][Gg]|[Ww][Aa][Rr][Mm][Ee][Rr]|[Gg][Oo][Ww][Nn]|[Bb][Ee][Dd]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Rr][Ee][Ss][Tt][Rr][Aa][Ii][Nn][Tt]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ee][Rr]|[Ww][Ee][Dd][Gg][Ee]|[Ee][Dd][Uu][Cc][Aa][Tt][Ii][Oo][Nn]|[Cc][Oo][Mm][Ff][Oo][Rr][Tt]|[Hh][Aa][Nn][Dd][Ll][Ii][Nn][Gg]|[Rr][Ee][Aa][Dd][Yy]|[Pp][Rr][Ee][Oo][Pp][Ee][Rr][Aa][Tt][Ii][Vv][Ee]|[Pp][Rr][Ee][Pp][Aa][Rr][Aa][Tt][Ii][Oo][Nn]|[Ss][Pp][Ee][Cc][Ii][Mm][Ee][Nn]|[Ss][Aa][Mm][Pp][Ll][Ee]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Ii][Nn][Ff][Oo][Rr][Mm][Aa][Tt][Ii][Oo][Nn]|[Ww][Rr][Ii][Ss][Tt][Bb][Aa][Nn][Dd]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Bb][Rr][Aa][Cc][Ee][Ll][Ee][Tt]|[Bb][Aa][Nn][Dd]|[Ww][Ii][Pp][Ee]|[Ii][Ss][Oo][Ll][Aa][Tt][Ii][Oo][Nn]|[Ss][Uu][Pp][Pp][Oo][Rr][Tt]|[Cc][Aa][Rr][Tt]|[Cc][Hh][Aa][Ii][Rr]|[Hh][Yy][Gg][Ii][Ee][Nn][Ee]|[Bb][Aa][Tt][Hh][Ii][Nn][Gg]|[Bb][Aa][Tt][Hh]|[Ss][Kk][Ii][Nn]|[Cc][Ll][Ee][Aa][Nn][Ss][Ee][Rr]|[Ss][Ll][Ii][Dd][Ee]|[Ss][Hh][Ee][Ee][Tt]|[Ss][Ll][Ii][Nn][Gg]|[Aa][Ii][Dd]|[Kk][Ii][Tt]|[Ss][Uu][Pp][Pp][Ll][Yy]|[Ss][Uu][Pp][Pp][Ll][Ii][Ee][Ss]|[Aa][Cc][Cc][Ee][Ss][Ss]|[Cc][Oo][Nn][Tt][Aa][Cc][Tt]|[Cc][Oo][Oo][Ll][Ii][Nn][Gg]|[Hh][Ee][Aa][Tt][Ii][Nn][Gg]|[Ss][Ii][Dd][Ee]|[Pp][Oo][Rr][Tt]|[Cc][Aa][Bb][Ll][Ee]|[Cc][Ii][Rr][Cc][Uu][Ii][Tt]|[Ii][Nn][Tt][Ee][Rr][Ff][Aa][Cc][Ee]|[Ss][Ii][Tt][Tt][Ee][Rr]|[Cc][Uu][Ff][Ff]|[Ll][Ii][Nn][Ee]|[Hh][Oo][Ss][Ee]|[Tt][Uu][Bb][Ii][Nn][Gg]|[Mm][Aa][Ss][Kk]|[Ww][Oo][Uu][Nn][Dd]|[Dd][Rr][Ee][Ss][Ss][Ii][Nn][Gg]|[Gg][Ll][Oo][Vv][Ee]|[Dd][Rr][Aa][Pp][Ee]|[Cc][Oo][Vv][Ee][Rr]|[Pp][Ii][Ll][Ll][Oo][Ww]|[Bb][Ll][Aa][Nn][Kk][Ee][Tt]|[Bb][Aa][Gg]|[Cc][Aa][Pp]|[Ll][Aa][Bb][Ee][Ll]|[Cc][Hh][Aa][Rr][Tt]|[Rr][Ee][Cc][Oo][Rr][Dd]|[Dd][Aa][Tt][Aa]|[Rr][Oo][Oo][Mm]|[Ll][Ee][Ff][Tt]|[Rr][Ii][Gg][Hh][Tt]|[Ii][Nn][Ss][Tt][Rr][Uu][Cc][Tt][Ii][Oo][Nn]|[Tt][Rr][Aa][Yy]|[Ss][Cc][Aa][Ll][Ee]|[Tt][Hh][Ee][Rr][Mm][Oo][Mm][Ee][Tt][Ee][Rr]|[Ee][Ll][Ee][Cc][Tt][Rr][Oo][Dd][Ee]|[Hh][Oo][Ii][Ss][Tt]|[Bb][Oo][Aa][Rr][Dd]|[Bb][Ee][Ll][Tt]|[Ss][Hh][Oo][Ee]|[Ss][Oo][Cc][Kk]|[Bb][Rr][Ii][Ee][Ff]|[Dd][Ii][Aa][Pp][Ee][Rr]|[Uu][Nn][Dd][Ee][Rr][Pp][Aa][Dd]|[Pp][Aa][Dd]|[Uu][Rr][Ii][Nn][Aa][Ll]|[Bb][Ee][Dd][Pp][Aa][Nn]|[Cc][Oo][Mm][Mm][Oo][Dd][Ee]|[Ww][Aa][Ll][Kk][Ee][Rr]|[Ww][Hh][Ee][Ee][Ll][Cc][Hh][Aa][Ii][Rr]|[Ss][Tt][Rr][Ee][Tt][Cc][Hh][Ee][Rr]|[Mm][Aa][Tt][Tt][Rr][Ee][Ss][Ss]|[Tt][Oo][Ww][Ee][Ll]|[Ww][Aa][Ss][Hh][Cc][Ll][Oo][Tt][Hh]|[Cc][Ll][Oo][Tt][Hh]|[Ww][Aa][Ss][Hh]|[Hh][Aa][Nn][Dd]|[Bb][Aa][Ss][Ii][Nn]|[Ii][Tt][Ee][Mm]|[Pp][Rr][Oo][Dd][Uu][Cc][Tt]|[Ss][Ee][Tt]|[Pp][Aa][Cc][Kk]|[Cc][Aa][Ll][Ll]|[Cc][Oo][Rr][Dd]|[Aa][Ll][Aa][Rr][Mm]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Cc][Rr][Uu][Bb]|[Cc][Uu][Ss][Hh][Ii][Oo][Nn]|[Tt][Aa][Bb][Ll][Ee]|[Ff][Oo][Ll][Dd][Ee][Rr]|[Tt][Rr][Aa][Nn][Ss][Pp][Oo][Rr][Tt]|[Oo][Xx][Yy][Gg][Ee][Nn]|[Ss][Uu][Cc][Tt][Ii][Oo][Nn]|[Cc][Aa][Nn][Ii][Ss][Tt][Ee][Rr]|[Gg][Aa][Ii][Tt]|[Ff][Aa][Ll][Ll]|[Mm][Aa][Tt]|[Pp][Rr][Oo][Tt][Ee][Cc][Tt][Ii][Oo][Nn]|[Tt][Uu][Rr][Nn]|[Aa][Ss][Ss][Ii][Ss][Tt]|[Vv][Aa][Ll][Uu][Aa][Bb][Ll][Ee]|[Pp][Rr][Ee][Ss][Ss][Uu][Rr][Ee]|[Ss][Ii][Mm][Uu][Ll][Aa][Tt][Oo][Rr]|[Gg][Aa][Uu][Zz][Ee]|[Nn][Ee][Ee][Dd][Ll][Ee]|[Ss][Yy][Rr][Ii][Nn][Gg][Ee]|[Bb][Aa][Nn][Dd][Aa][Gg][Ee]|[Tt][Aa][Pp][Ee]|[Hh][Ee][Aa][Rr][Ii][Nn][Gg]|[Hh][Aa][Ii][Rr]|[Bb][Ii][Bb]|[Tt][Ee][Mm][Pp][Ee][Rr][Aa][Tt][Uu][Rr][Ee]|[Pp][Rr][Oo][Bb][Ee]|[Ss][Ee][Nn][Ss][Oo][Rr]|[Ll][Ee][Aa][Dd]|[Ss][Yy][Ss][Tt][Ee][Mm]|[Pp][Uu][Mm][Pp]|[Aa][Nn][Aa][Ll][Gg][Ee][Ss][Ii][Aa]|[Cc][Aa][Tt][Hh][Ee][Tt][Ee][Rr]|[Bb][Oo][Tt][Tt][Ll][Ee]|[Cc][Oo][Nn][Tt][Aa][Ii][Nn][Ee][Rr]|[Cc][Uu][Pp]|[Pp][Ii][Tt][Cc][Hh][Ee][Rr]|[Tt][Uu][Bb][Ee]|[Vv][Aa][Ll][Vv][Ee]|[Ff][Ii][Ll][Tt][Ee][Rr]|[Aa][Dd][Aa][Pp][Tt][Ee][Rr]|[Cc][Oo][Nn][Nn][Ee][Cc][Tt][Oo][Rr]|[Cc][Ll][Ii][Pp]|[Ss][Tt][Rr][Aa][Pp]|[Hh][Aa][Rr][Nn][Ee][Ss][Ss]|[Vv][Ee][Ss][Tt]|[Gg][Aa][Rr][Mm][Ee][Nn][Tt]|[Ss][Ll][Ee][Ee][Vv][Ee]|[Ss][Tt][Oo][Cc][Kk][Ii][Nn][Gg]|[Bb][Oo][Oo][Tt]|[Ss][Pp][Ll][Ii][Nn][Tt]|[Bb][Rr][Aa][Cc][Ee]|[Cc][Oo][Ll][Ll][Aa][Rr]|[Rr][Aa][Ii][Ll]|[Pp][Oo][Ll][Ee]|[Ss][Tt][Aa][Nn][Dd]|[Ll][Aa][Mm][Pp]|[Ll][Ii][Gg][Hh][Tt]|[Cc][Aa][Mm][Ee][Rr][Aa]|[Pp][Hh][Oo][Nn][Ee]|[Rr][Ee][Mm][Oo][Tt][Ee]|[Cc][Oo][Nn][Tt][Rr][Oo][Ll]|[Bb][Uu][Tt][Tt][Oo][Nn]|[Ss][Ww][Ii][Tt][Cc][Hh]|[Ss][Cc][Rr][Ee][Ee][Nn]|[Dd][Ii][Ss][Pp][Ll][Aa][Yy]|[Ss][Oo][Ff][Tt][Ww][Aa][Rr][Ee]|[Pp][Rr][Oo][Gg][Rr][Aa][Mm]|[Pp][Ll][Aa][Nn]|[Ff][Oo][Rr][Mm]|[Ss][Tt][Ii][Cc][Kk][Ee][Rr]|[Ss][Tt][Aa][Mm][Pp]|[Mm][Aa][Rr][Kk][Ee][Rr]|[Pp][Ee][Nn]|[Pp][Ee][Nn][Cc][Ii][Ll]|[Pp][Aa][Pp][Ee][Rr]|[Ee][Nn][Vv][Ee][Ll][Oo][Pp][Ee]|[Ff][Ii][Ll][Ee]|[Bb][Ii][Nn][Dd][Ee][Rr]|[Ss][Tt][Oo][Oo][Ll]|[Cc][Hh][Aa][Rr][Tt][Ii][Nn][Gg]|[Hh][Oo][Ll][Dd][Ee][Rr]|[Pp][Rr][Ii][Vv][Aa][Cc][Yy]|[Ww][Ee][Ii][Gg][Hh][Ii][Nn][Gg]|[Cc][Oo][Mm][Pp][Rr][Ee][Ss][Ss][Ii][Oo][Nn]|[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Ii][Nn][Gg]|[Pp][Rr][Ee][Pp]|[Dd][Ee][Vv][Ii][Cc][Ee]|[Ll][Aa][Tt][Ee][Rr][Aa][Ll]|[Rr][Ee][Ss][Uu][Ss][Cc][Ii][Tt][Aa][Tt][Oo][Rr]|[Vv][Ee][Ss][Tt]|[Ss][Kk][Ii][Nn]|[Ss][Aa][Ff][Ee][Tt][Yy]|[Ss][Aa][Tt][Ii][Ss][Ff][Aa][Cc][Tt][Ii][Oo][Nn]|[Ss][Uu][Rr][Vv][Ee][Yy]|[Bb][Ii][Ll][Ll]|[Rr][Ii][Gg][Hh][Tt][Ss]|[Pp][Oo][Rr][Tt][Aa][Ll]|[Ii][Dd][Ee][Nn][Tt][Ii][Ff][Ii][Cc][Aa][Tt][Ii][Oo][Nn]|[Aa][Rr][Mm][Bb][Aa][Nn][Dd]|[Hh][Oo][Ss][Pp][Ii][Tt][Aa][Ll]|[Ii][Nn][Ff][Oo]|[Aa][Dd][Mm][Ii][Ss][Ss][Ii][Oo][Nn]|[Ss][Hh][Oo][Ww][Ee][Rr]|[Nn][Aa][Ss][Aa][Ll]|[Dd][Ee][Nn][Tt][Aa][Ll]|[Dd][Ii][Gg][Nn][Ii][Tt][Yy]|[Ss][Ww][Aa][Bb]|[Vv][Oo][Mm][Ii][Tt]|[Ss][Hh][Aa][Rr][Pp]|[Dd][Ii][Ss][Pp][Oo][Ss][Aa][Ll]|[Hh][Aa][Nn][Dd][Bb][Oo][Oo][Kk]|[Ww][Tt]|[Ss][Pp][Ii][Rr][Oo][Mm][Ee][Tt][Ee][Rr])(?:[Ee]?[Ss])?\y)[A-Za-zÀ-ɏͰ-ϿЀ-ӿḀ-ỿ''\u2019-]+(?![A-Za-zÀ-ɏͰ-ϿЀ-ӿḀ-ỿ0-9]))?(?=\.?\s*$|\s*[,;]|\s+(?:[Dd][Oo][Bb]|[Mm][Rr][Nn]|[Ii][Dd])\y|\s*[0-9]))'
$f$;

-- Date of birth after DOB / born (on), numeric or with a month name. Mirrors DOB_PATTERN.
CREATE OR REPLACE FUNCTION b4p_dob_pattern() RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT '(?i)\y(?:dob|d\.o\.b\.?|date of birth|born(?:\s+on)?)[\s:]*(?:[0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+[0-9]{1,2}(?:st|nd|rd|th)?,?\s+[0-9]{2,4}|[0-9]{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+[0-9]{2,4})'
$f$;

-- Text as the patterns should see it: zero-width / bidi / soft-hyphen characters
-- removed, full-width and other compatibility forms folded (NFKC), dash and
-- Cyrillic/Greek look-alikes turned into plain ASCII. Mirrors fold().
CREATE OR REPLACE FUNCTION b4p_fold(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $f$
    SELECT translate(normalize(regexp_replace(t,
        '[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]', '', 'g'), NFKC),
        '‐‑‒–—―−аеорсхуіјѕкмтнвАВЕКМНОРСТХУІЈЅορνταεικυχΑΒΕΖΗΙΚΜΝΟΡΤΥΧ',
        '-------aeopcxyijskmtnbABEKMHOPCTXYIJSopvtaeikuxABEZHIKMNOPTYX')
$f$;

-- The replacements themselves (on already folded text).
CREATE OR REPLACE FUNCTION b4p_redact_raw(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $f$
    SELECT regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
           regexp_replace(regexp_replace(t,
        '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[email redacted]', 'g'),
        '\y(?:[Mm][Rr][Nn]|[Mm][Rr]#|[Mm]ed(?:ical)?\s*[Rr]ec(?:ord)?\s*(?:#|[Nn]o\.?|[Nn]umber))[\s:#.]*[A-Za-z0-9-]*[0-9][A-Za-z0-9-]*',
        'MRN [redacted]', 'g'),
        b4p_dob_pattern(), 'DOB [redacted]', 'g'),
        b4p_patient_id_pattern(), 'Pt. ID [redacted]', 'g'),
        b4p_patient_name_pattern(), 'Pt. [name redacted]', 'g'),
        b4p_patient_surname_pattern(), 'Pt. [name redacted]', 'g'),
        b4p_subject_id_pattern(), '[id redacted]', 'g'),
        '\y[0-9]{3}-[0-9]{2}-[0-9]{4}\y', '[ssn redacted]', 'g'),
        '(?:\+?1[\s.-]?)?(?:\([0-9]{3}\)\s?|\y[0-9]{3}[\s.-])[0-9]{3}[\s.-][0-9]{4}\y', '[phone redacted]', 'g')
$f$;

-- Text with personal identifiers replaced: patient IDs and names, research-subject
-- IDs, MRNs, dates of birth, email addresses and US phone numbers. Text with
-- nothing to redact comes back exactly as given.
CREATE OR REPLACE FUNCTION redact_phi(t TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $f$
DECLARE
    f TEXT;
    r TEXT;
BEGIN
    f := CASE WHEN t ~ '^[\x01-\x7f]*$' THEN t ELSE b4p_fold(t) END;  -- plain ASCII needs no folding
    -- cheap pre-check: nearly all text has none of the trigger words (speed matters, views call this a lot)
    IF lower(f) !~ 'pt|patient|mrn|mr#|rec|dob|d\.o\.b|birth|born|@|[0-9][^0-9a-z]+[0-9]{4}|[a-z][0-9]{2}\s*-\s*[0-9]{3}' THEN
        RETURN t;
    END IF;
    r := b4p_redact_raw(f);
    RETURN CASE WHEN r = f THEN t ELSE r END;
END
$f$;

-- true if redact_phi would change any of the texts
CREATE OR REPLACE FUNCTION b4p_has_phi(VARIADIC t TEXT[]) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT EXISTS (SELECT 1 FROM unnest(t) x WHERE x IS NOT NULL AND redact_phi(x) <> x)
$f$;

-- Dimension names, redacted once per query instead of once per inventory row.
CREATE OR REPLACE VIEW b4p_safe_category AS
SELECT category_id, name::TEXT AS n, redact_phi(name::TEXT) AS rn FROM category;
CREATE OR REPLACE VIEW b4p_safe_manufacturer AS
SELECT manufacturer_id, name::TEXT AS n, redact_phi(name::TEXT) AS rn FROM manufacturer;
CREATE OR REPLACE VIEW b4p_safe_box AS
SELECT box_id, box_type::TEXT AS n, redact_phi(box_type::TEXT) AS rn, box_number, pallet_number FROM box;

-- true if ANY text cell of a sheet row (all columns but the photo link) has something to redact.
-- Every view uses this one test to decide whether to hide a row's photo.
CREATE OR REPLACE FUNCTION b4p_raw_has_phi(raw JSONB) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
    SELECT EXISTS (SELECT 1 FROM jsonb_each_text(raw) e
                   WHERE e.key <> 'Image URL' AND e.value IS NOT NULL AND redact_phi(e.value) <> e.value)
$f$;

-- Human-friendly inventory (active rows only). Free text is redacted.
CREATE OR REPLACE VIEW v_inventory AS
SELECT i.item_id, i.sheet_row,
       redact_phi(COALESCE(s.name, NULLIF(i.raw_name, ''))) AS name,
       x.rgen AS general_name,
       m.rn AS manufacturer,
       c.rn AS category,
       i.quantity, x.rlot AS lot_number, i.expiration_date,
       (i.expiration_date < CURRENT_DATE) AS is_expired,
       i.warehouse_date,
       CURRENT_DATE - i.warehouse_date AS days_in_warehouse,
       b.rn AS box_type, b.box_number, b.pallet_number,
       i.flagged, x.rrev AS review,
       CASE WHEN b4p_raw_has_phi(i.raw) THEN NULL ELSE i.image_url END AS image_url,
       x.rnotes AS notes
FROM inventory_item i
LEFT JOIN supply s       ON s.supply_id = i.supply_id
LEFT JOIN b4p_safe_manufacturer m ON m.manufacturer_id = i.manufacturer_id
JOIN b4p_safe_category c ON c.category_id = i.category_id
LEFT JOIN b4p_safe_box b ON b.box_id = i.box_id
CROSS JOIN LATERAL (SELECT redact_phi(i.raw_name) AS rname, redact_phi(i.general_name) AS rgen,
                           redact_phi(i.notes) AS rnotes, redact_phi(i.lot_number) AS rlot,
                           redact_phi(i.review) AS rrev) x
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
       CASE WHEN i.raw_name = '' THEN 'Item ' || (i.sheet_row - 1) ELSE x.rname END AS name,
       COALESCE(x.rgen, '')                                              AS gen_name,
       c.rn                                                              AS category,
       COALESCE(m.rn, 'Unknown')                                         AS manufacturer,
       NULLIF(x.rrev, '')                                                AS review_status,
       (CURRENT_DATE - i.warehouse_date)::INT                            AS days_in_warehouse,
       CASE WHEN b4p_raw_has_phi(i.raw) THEN NULL ELSE i.image_url END                               AS image_url,
       i.quantity,
       COALESCE(i.unit_value, s.unit_value_usd, 0)::FLOAT8               AS unit_value,
       (i.quantity * COALESCE(i.unit_value, s.unit_value_usd, 0))::FLOAT8 AS total_value,
       to_char(i.expiration_date, 'YYYY-MM-DD')                          AS expiration_date,
       to_char(i.warehouse_date, 'YYYY-MM-DD')                           AS warehouse_date,
       COALESCE(b.pallet_number, 0)                                      AS pallet_number,
       COALESCE(b.rn, 'Unassigned')                                      AS category_box,
       COALESCE(b.box_number, 0)                                         AS box_number,
       i.sheet_row,
       x.rlot                                                            AS lot_number
FROM inventory_item i
JOIN b4p_safe_category c ON c.category_id = i.category_id
LEFT JOIN b4p_safe_manufacturer m ON m.manufacturer_id = i.manufacturer_id
LEFT JOIN supply s       ON s.supply_id = i.supply_id
LEFT JOIN b4p_safe_box b ON b.box_id = i.box_id
CROSS JOIN LATERAL (SELECT redact_phi(i.raw_name) AS rname, redact_phi(i.general_name) AS rgen,
                           redact_phi(i.notes) AS rnotes, redact_phi(i.lot_number) AS rlot,
                           redact_phi(i.review) AS rrev) x
WHERE i.removed_at IS NULL
ORDER BY i.sheet_row, i.item_id;

-- External dashboard: same keys/cell text as its /api/supplies (Google Sheet) rows.
CREATE OR REPLACE VIEW dash_supplies AS
SELECT COALESCE(x.man, '')   AS "Manufacturer Name",
       COALESCE(x.cat, '')   AS "Category",
       COALESCE(x.sub, '')   AS "Subcategory",
       COALESCE(x.gen, '')   AS "General",
       COALESCE(x.nam, '')   AS "Name",
       COALESCE(x.lot, '')   AS "Lot Number",
       COALESCE(x.nts, '')   AS "Notes",
       i.quantity::TEXT      AS "Quantity",  -- the number we parsed (or set), never the raw cell
       COALESCE(to_char(i.expiration_date, 'YYYY-MM-DD'), x.exp, '') AS "Date of Expiration",
       COALESCE(x.pal, '')   AS "Pallet Number",
       COALESCE(x.box, '')   AS "Box Number",
       CASE WHEN b4p_raw_has_phi(i.raw) THEN '' ELSE COALESCE(i.raw ->> 'Image URL', '') END AS "Image URL",
       COALESCE(x.rev, '')   AS "Review",
       CASE WHEN COALESCE(i.raw ->> 'Days in Warehouse', '') ~ '^\s*[0-9]+\s*$'
            THEN (CURRENT_DATE - i.warehouse_date)::TEXT   -- the sheet's own cell goes stale between syncs; this ticks
            ELSE COALESCE(x.dys, '') END AS "Days in Warehouse",
       i.sheet_row           AS "_sheet_row"
FROM inventory_item i
CROSS JOIN LATERAL (SELECT i.raw ->> 'Manufacturer Name' AS man, i.raw ->> 'Category' AS cat,
                           COALESCE(i.raw ->> 'Subcategory', i.raw ->> 'Subcategories') AS sub,
                           i.raw ->> 'General' AS gen, i.raw ->> 'Name' AS nam, i.raw ->> 'Lot Number' AS lot,
                           i.raw ->> 'Notes' AS nts, i.raw ->> 'Review' AS rev) r
CROSS JOIN LATERAL (SELECT redact_phi(r.man) AS man, redact_phi(r.cat) AS cat, redact_phi(r.sub) AS sub,
                           redact_phi(r.gen) AS gen, redact_phi(r.nam) AS nam, redact_phi(r.lot) AS lot,
                           redact_phi(r.nts) AS nts, redact_phi(r.rev) AS rev,
                           redact_phi(i.raw ->> 'Date of Expiration') AS exp,
                           redact_phi(i.raw ->> 'Pallet Number') AS pal, redact_phi(i.raw ->> 'Box Number') AS box,
                           redact_phi(i.raw ->> 'Days in Warehouse') AS dys) x
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
       CASE WHEN si.review_flag THEN 'TRUE' ELSE 'FALSE' END AS "Review Flag",
       COALESCE(si.item_id::TEXT, '')        AS "Item ID"      -- dash_items.id the requester picked, or ''
FROM shipment sh
JOIN requester r      ON r.requester_id = sh.requester_id
JOIN shipment_item si ON si.shipment_id = sh.shipment_id
ORDER BY sh.requested_at, sh.request_id, si.line_no;

-- One row: when did the data last refresh? Runs that could not reach the sheet
-- (source 'unavailable...') or that failed are never counted as successes.
CREATE OR REPLACE VIEW dash_sync_status AS
SELECT (SELECT max(finished_at) FROM sync_run
         WHERE error IS NULL AND refused IS NULL AND finished_at IS NOT NULL
           AND COALESCE(source, '') NOT LIKE 'unavailable%')      AS last_success_at,
       l.started_at                                               AS last_run_at,
       CASE WHEN l.run_id IS NULL THEN NULL
            ELSE (l.error IS NULL AND l.refused IS NULL AND l.finished_at IS NOT NULL
                  AND COALESCE(l.source, '') NOT LIKE 'unavailable%') END AS last_run_ok,
       COALESCE(jsonb_array_length(l.warnings), 0)::INT           AS last_warning_count,
       (SELECT count(*)::INT FROM inventory_item WHERE removed_at IS NULL) AS active_items,
       CASE WHEN l.run_id IS NULL THEN NULL ELSE l.refused IS NOT NULL END AS last_run_refused
FROM (SELECT 1) one
LEFT JOIN LATERAL (SELECT * FROM sync_run ORDER BY started_at DESC, run_id DESC LIMIT 1) l ON true;

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
--   item_id: optional dash_items.id (string or number); unknown ids are ignored
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
    v_ref       BIGINT;
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
        -- optional "item_id" (dash_items.id): kept only if it names an active inventory row;
        -- anything else (unknown or removed row, wrong type, junk) is ignored rather than failing the request
        v_ref := NULL;
        IF btrim(COALESCE(v_item ->> 'item_id', '')) ~ '^[0-9]{1,18}$' THEN
            SELECT item_id INTO v_ref FROM inventory_item
             WHERE item_id = btrim(v_item ->> 'item_id')::BIGINT AND removed_at IS NULL;
        END IF;
        INSERT INTO shipment_item (shipment_id, line_no, item_name, category,
                                   quantity_requested, review_flag, item_id)
        VALUES (v_shipment, v_line, btrim(v_item ->> 'item_name'),
                COALESCE(v_item ->> 'category', ''), (v_item ->> 'quantity')::NUMERIC::INTEGER,
                COALESCE((v_item ->> 'review_flag')::BOOLEAN, false), v_ref);
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
-- PROPOSAL: stock taken out of inventory when a request is marked Shipped (one row per
-- inventory item used; a line that could not be filled completely gets an extra row with
-- item_id NULL and short_qty = what was missing). Rows are never changed afterwards.
CREATE TABLE IF NOT EXISTS shipment_pick (
    pick_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id       TEXT NOT NULL,
    shipment_item_id BIGINT REFERENCES shipment_item ON DELETE SET NULL,
    line_no          INTEGER NOT NULL,
    item_id          BIGINT REFERENCES inventory_item ON DELETE SET NULL,
    qty              INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
    short_qty        INTEGER NOT NULL DEFAULT 0 CHECK (short_qty >= 0),
    picked_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shipment_pick_request ON shipment_pick(request_id);
-- an ops shipment (dash_shipment.id) that counted this pick as part of its own stock (see b4p_ship_stock)
ALTER TABLE shipment_pick ADD COLUMN IF NOT EXISTS claimed_by TEXT;

-- Take the stock for a shipped request (called once, when the status becomes Shipped).
-- A line's item_name may end in "[Lot X, Box Y, Pallet Z]": rows matching those come first,
-- then the soonest expiration (FEFO). Quantities never go below 0. The new quantity is kept
-- like a dashboard edit (quantity_set_at) until someone edits that sheet row's Quantity cell.
CREATE OR REPLACE FUNCTION b4p_take_stock(p_request_id TEXT) RETURNS VOID
LANGUAGE plpgsql AS $f$
DECLARE
    v_line   RECORD;
    v_cand   RECORD;
    v_base   TEXT;
    v_lot    TEXT;
    v_box    TEXT;
    v_pallet TEXT;
    v_m      TEXT[];
    v_need   INT;
    v_take   INT;
BEGIN
    IF EXISTS (SELECT 1 FROM shipment_pick WHERE request_id = p_request_id) THEN
        RETURN;   -- already taken: never twice
    END IF;
    IF EXISTS (SELECT 1 FROM dash_shipment WHERE p_request_id = ANY (request_ids)) THEN
        RETURN;   -- an ops shipment carries this request: it takes the stock when it ships (b4p_ship_stock)
    END IF;
    FOR v_line IN
        SELECT si.shipment_item_id, si.line_no, si.item_name, si.quantity_requested
          FROM shipment_item si JOIN shipment sh USING (shipment_id)
         WHERE sh.request_id = p_request_id ORDER BY si.line_no
    LOOP
        v_m := regexp_match(v_line.item_name, '\s*\[Lot ([^,\]]*),\s*Box ([^,\]]*),\s*Pallet ([^\]]*)\]\s*$', 'i');
        v_base   := btrim(CASE WHEN v_m IS NULL THEN v_line.item_name
                               ELSE regexp_replace(v_line.item_name, '\s*\[Lot [^\]]*\]\s*$', '', 'i') END);
        v_lot    := NULLIF(btrim(v_m[1]), '');
        v_box    := NULLIF(btrim(v_m[2]), '');
        v_pallet := NULLIF(btrim(v_m[3]), '');
        v_need := v_line.quantity_requested;
        FOR v_cand IN
            SELECT i.item_id, i.quantity
              FROM inventory_item i LEFT JOIN box b ON b.box_id = i.box_id
             WHERE i.removed_at IS NULL AND i.quantity > 0
               AND lower(btrim(i.raw_name)) = lower(v_base)
             ORDER BY (v_lot IS NOT NULL AND i.lot_number IS NOT DISTINCT FROM v_lot) DESC,
                      (v_box IS NOT NULL AND b.box_number::TEXT = v_box) DESC,
                      (v_pallet IS NOT NULL AND b.pallet_number::TEXT = v_pallet) DESC,
                      i.expiration_date ASC NULLS LAST, i.item_id
               FOR UPDATE OF i
        LOOP
            EXIT WHEN v_need <= 0;
            v_take := LEAST(v_cand.quantity, v_need);
            UPDATE inventory_item SET quantity = quantity - v_take, quantity_set_at = clock_timestamp(),
                                      updated_at = now() WHERE item_id = v_cand.item_id;
            INSERT INTO shipment_pick (request_id, shipment_item_id, line_no, item_id, qty)
            VALUES (p_request_id, v_line.shipment_item_id, v_line.line_no, v_cand.item_id, v_take);
            v_need := v_need - v_take;
        END LOOP;
        IF v_need > 0 THEN   -- not enough stock (or no matching item): record the shortfall
            INSERT INTO shipment_pick (request_id, shipment_item_id, line_no, item_id, qty, short_qty)
            VALUES (p_request_id, v_line.shipment_item_id, v_line.line_no, NULL, 0, v_need);
        END IF;
    END LOOP;
END $f$;

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
    IF v_new = 'Shipped' THEN
        PERFORM b4p_take_stock(btrim(p_request_id));   -- PROPOSAL, see docs/CHANGES-2026-10-11.md
    END IF;
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

-- ===========================================================================
-- OPS SHIPMENTS  (docs/CONTRACT.md, "Ops shipments")
-- ===========================================================================

-- Redacted header rows. Text columns go through redact_phi like every other view.
CREATE OR REPLACE VIEW dash_shipments AS
SELECT s.id,
       redact_phi(s.name)                 AS name,
       redact_phi(s.recipient)            AS recipient,
       redact_phi(s.shipment_type)        AS shipment_type,
       redact_phi(s.shipping_method)      AS shipping_method,
       redact_phi(s.blueprints_contact)   AS blueprints_contact,
       s.stage,
       to_char(s.ship_date, 'YYYY-MM-DD')       AS ship_date,
       to_char(s.delivered_date, 'YYYY-MM-DD')  AS delivered_date,
       ARRAY(SELECT redact_phi(r) FROM unnest(s.request_ids) WITH ORDINALITY u(r, n) ORDER BY n) AS request_ids,
       redact_phi(s.notes)                AS notes,
       s.kind,
       s.unit_count,
       s.value::FLOAT8                    AS value,
       b4p_iso_utc(s.created_at)          AS created_at,
       b4p_iso_utc(s.updated_at)          AS updated_at
FROM dash_shipment s
ORDER BY s.created_at DESC, s.id;

-- Units promised to shipments that have not shipped yet, per inventory item.
CREATE OR REPLACE VIEW dash_allocations AS
SELECT l.item_id::TEXT          AS item_id,
       s.id                     AS shipment_id,
       redact_phi(s.name)       AS shipment_name,
       s.stage,
       l.quantity
FROM dash_shipment_line l
JOIN dash_shipment s ON s.id = l.shipment_id
WHERE s.stage IN ('planned', 'pulling', 'consolidated') AND l.item_id IS NOT NULL
ORDER BY l.item_id, s.created_at, s.id;

-- One row per shipment line with where to find the item and how much is left to promise.
--   on_hand          current inventory quantity of the item
--   allocated_active units promised to all active shipments (including this line)
--   available        on_hand - allocated_active (can be negative if the sheet quantity dropped)
--   max_quantity     the most this line could be raised to (quantity + available, at least 0)
--   expiry_status    expired | d30 | d90 | later | none, as of the shipment's ship_date (today if unset)
-- For a shipped/delivered line available/max_quantity are only informative.
CREATE OR REPLACE VIEW dash_shipment_lines AS
SELECT l.id::TEXT                AS id,
       l.shipment_id,
       l.item_id::TEXT           AS item_id,
       l.quantity,
       l.picked,
       l.consolidated,
       CASE WHEN i.item_id IS NULL THEN redact_phi(l.description)
            WHEN i.raw_name = '' THEN 'Item ' || (i.sheet_row - 1) ELSE x.rname END AS name,
       c.rn                      AS category,
       CASE WHEN i.item_id IS NULL THEN redact_phi(l.lot_snapshot) ELSE x.rlot END AS lot_number,
       to_char(CASE WHEN i.item_id IS NULL THEN l.exp_snapshot ELSE i.expiration_date END, 'YYYY-MM-DD') AS expiration_date,
       CASE WHEN i.item_id IS NULL THEN NULL ELSE COALESCE(b.pallet_number, 0) END     AS pallet_number,
       CASE WHEN i.item_id IS NULL THEN NULL ELSE COALESCE(b.rn, 'Unassigned') END      AS category_box,
       CASE WHEN i.item_id IS NULL THEN NULL ELSE COALESCE(b.box_number, 0) END         AS box_number,
       COALESCE(l.unit_price, i.unit_value, sp.unit_value_usd, 0)::FLOAT8               AS unit_price,
       i.quantity                AS on_hand,
       a.allocated               AS allocated_active,
       (i.quantity - a.allocated)::INT          AS available,
       CASE WHEN i.item_id IS NULL THEN NULL ELSE GREATEST(l.quantity + i.quantity - a.allocated, 0)::INT END AS max_quantity,
       CASE WHEN e.d IS NULL THEN 'none'
            WHEN e.d < COALESCE(s.ship_date, CURRENT_DATE) THEN 'expired'
            WHEN e.d <= COALESCE(s.ship_date, CURRENT_DATE) + 30 THEN 'd30'
            WHEN e.d <= COALESCE(s.ship_date, CURRENT_DATE) + 90 THEN 'd90'
            ELSE 'later' END     AS expiry_status
FROM dash_shipment_line l
JOIN dash_shipment s ON s.id = l.shipment_id
LEFT JOIN inventory_item i ON i.item_id = l.item_id
LEFT JOIN supply sp ON sp.supply_id = i.supply_id
LEFT JOIN b4p_safe_category c ON c.category_id = i.category_id
LEFT JOIN b4p_safe_box b ON b.box_id = i.box_id
CROSS JOIN LATERAL (SELECT redact_phi(i.raw_name) AS rname, redact_phi(i.lot_number) AS rlot) x
CROSS JOIN LATERAL (SELECT CASE WHEN i.item_id IS NULL THEN l.exp_snapshot ELSE i.expiration_date END AS d) e
LEFT JOIN LATERAL (SELECT COALESCE(sum(l2.quantity), 0)::INT AS allocated
                      FROM dash_shipment_line l2 JOIN dash_shipment s2 ON s2.id = l2.shipment_id
                     WHERE l2.item_id = l.item_id
                       AND s2.stage IN ('planned', 'pulling', 'consolidated')) a ON i.item_id IS NOT NULL
ORDER BY l.shipment_id, l.id;

CREATE OR REPLACE FUNCTION b4p_stage_rank(p TEXT) RETURNS INT LANGUAGE sql IMMUTABLE AS $$
    SELECT array_position(ARRAY['planned', 'pulling', 'consolidated', 'shipped', 'delivered'], p)
$$;

-- Validates and normalizes the header keys present in a JSON object. Returns the object
-- with cleaned values (unknown keys raise). Used by create_shipment and update_shipment.
CREATE OR REPLACE FUNCTION b4p_shipment_header(p JSONB) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_out  JSONB := '{}';
    v_key  TEXT;
    v_val  JSONB;
    v_txt  TEXT;
    v_ids  TEXT[];
    v_max  INT;
BEGIN
    IF p IS NULL OR jsonb_typeof(p) <> 'object' THEN
        RAISE EXCEPTION 'shipment fields must be a JSON object';
    END IF;
    FOR v_key, v_val IN SELECT * FROM jsonb_each(p) LOOP
        IF v_key IN ('name', 'recipient', 'shipment_type', 'shipping_method', 'blueprints_contact', 'notes') THEN
            IF jsonb_typeof(v_val) = 'null' THEN
                v_txt := '';
            ELSIF jsonb_typeof(v_val) <> 'string' THEN
                RAISE EXCEPTION '% must be a string', v_key;
            ELSE
                v_txt := btrim(v_val #>> '{}');
            END IF;
            v_max := CASE WHEN v_key = 'notes' THEN 5000 ELSE 200 END;
            IF char_length(v_txt) > v_max THEN
                RAISE EXCEPTION '% is too long', v_key;
            END IF;
            IF v_key = 'name' AND v_txt = '' THEN
                RAISE EXCEPTION 'name is required';
            END IF;
            v_out := v_out || jsonb_build_object(v_key, v_txt);
        ELSIF v_key = 'kind' THEN
            IF jsonb_typeof(v_val) <> 'string' OR (v_val #>> '{}') NOT IN ('inventory', 'non_inventory') THEN
                RAISE EXCEPTION 'kind must be inventory or non_inventory';
            END IF;
            v_out := v_out || jsonb_build_object(v_key, v_val #>> '{}');
        ELSIF v_key IN ('unit_count', 'value') THEN
            IF jsonb_typeof(v_val) = 'null' THEN
                v_out := v_out || jsonb_build_object(v_key, NULL);
            ELSIF jsonb_typeof(v_val) <> 'number' OR (v_val #>> '{}')::NUMERIC < 0
                  OR (v_key = 'unit_count' AND (v_val #>> '{}') !~ '^\d{1,9}$')
                  OR (v_key = 'value' AND (v_val #>> '{}')::NUMERIC >= 10000000000) THEN
                RAISE EXCEPTION '% must be a non-negative number', v_key;
            ELSE
                v_out := v_out || jsonb_build_object(v_key, v_val);
            END IF;
        ELSIF v_key IN ('ship_date', 'delivered_date') THEN
            IF jsonb_typeof(v_val) = 'null' OR btrim(v_val #>> '{}') = '' THEN
                v_out := v_out || jsonb_build_object(v_key, NULL);
            ELSIF jsonb_typeof(v_val) <> 'string' OR (v_val #>> '{}') !~ '^\d{4}-\d{2}-\d{2}' THEN
                RAISE EXCEPTION '% must be YYYY-MM-DD', v_key;
            ELSE
                BEGIN
                    v_out := v_out || jsonb_build_object(v_key, left(v_val #>> '{}', 10)::DATE::TEXT);
                EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN
                    RAISE EXCEPTION '% must be a real date (YYYY-MM-DD)', v_key;
                END;
            END IF;
        ELSIF v_key = 'request_ids' THEN
            IF jsonb_typeof(v_val) = 'null' THEN
                v_ids := '{}';
            ELSIF jsonb_typeof(v_val) <> 'array' THEN
                RAISE EXCEPTION 'request_ids must be an array of strings';
            ELSE
                IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_val) e WHERE jsonb_typeof(e) <> 'string') THEN
                    RAISE EXCEPTION 'request_ids must be an array of strings';
                END IF;
                SELECT COALESCE(array_agg(r ORDER BY n), '{}') INTO v_ids
                  FROM (SELECT r, min(n) AS n
                          FROM (SELECT btrim(e #>> '{}') AS r, n FROM jsonb_array_elements(v_val) WITH ORDINALITY t(e, n)) q
                         WHERE r <> '' GROUP BY r) d;
                IF cardinality(v_ids) > 50 OR EXISTS (SELECT 1 FROM unnest(v_ids) r WHERE char_length(r) > 100) THEN
                    RAISE EXCEPTION 'request_ids: at most 50 ids of at most 100 characters';
                END IF;
            END IF;
            v_out := v_out || jsonb_build_object(v_key, to_jsonb(v_ids));
        ELSIF v_key = 'stage' THEN
            IF jsonb_typeof(v_val) <> 'string' OR b4p_stage_rank(lower(btrim(v_val #>> '{}'))) IS NULL THEN
                RAISE EXCEPTION 'unknown stage %', v_val #>> '{}';
            END IF;
            v_out := v_out || jsonb_build_object(v_key, lower(btrim(v_val #>> '{}')));
        ELSIF v_key IN ('created_at', 'updated_at') THEN
            NULL;   -- accepted and ignored here (import passes created_at separately)
        ELSE
            RAISE EXCEPTION 'unknown shipment field %', v_key;
        END IF;
    END LOOP;
    RETURN v_out;
END $$;

-- Check that raising item p_item to p_qty units on shipment p_ship does not promise more
-- than is on hand (other active shipments counted). Caller holds the inventory row lock.
-- Raises check_violation with DETAIL = {"item_id","requested","available"}.
CREATE OR REPLACE FUNCTION b4p_check_allocation(p_item BIGINT, p_ship TEXT, p_qty INT) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    v_on_hand INT;
    v_other   INT;
    v_name    TEXT;
BEGIN
    SELECT quantity, redact_phi(raw_name) INTO v_on_hand, v_name FROM inventory_item WHERE item_id = p_item;
    SELECT COALESCE(sum(l.quantity), 0) INTO v_other
      FROM dash_shipment_line l JOIN dash_shipment s ON s.id = l.shipment_id
     WHERE l.item_id = p_item AND l.shipment_id <> p_ship AND s.stage IN ('planned', 'pulling', 'consolidated');
    IF p_qty > v_on_hand - v_other THEN
        RAISE EXCEPTION 'only % available of "%" (item %): % requested, % on hand, % already in other shipments',
            GREATEST(v_on_hand - v_other, 0), v_name, p_item, p_qty, v_on_hand, v_other
            USING ERRCODE = 'check_violation',
                  DETAIL = json_build_object('item_id', p_item::TEXT, 'requested', p_qty,
                                             'available', GREATEST(v_on_hand - v_other, 0))::TEXT;
    END IF;
END $$;

-- Validates one line object's quantity / price / free-text fields; returns them normalized.
CREATE OR REPLACE FUNCTION b4p_line_fields(p_el JSONB) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_out JSONB;
    v_d   TEXT;
BEGIN
    IF jsonb_typeof(p_el) <> 'object' THEN
        RAISE EXCEPTION 'each line must be a JSON object';
    END IF;
    IF jsonb_typeof(p_el -> 'quantity') <> 'number' OR (p_el ->> 'quantity') !~ '^\d{1,6}$' OR (p_el ->> 'quantity')::INT < 1 THEN
        RAISE EXCEPTION 'quantity must be a whole number between 1 and 999999';
    END IF;
    IF (p_el ? 'unit_price' AND jsonb_typeof(p_el -> 'unit_price') NOT IN ('number', 'null'))
       OR (jsonb_typeof(p_el -> 'unit_price') = 'number' AND ((p_el ->> 'unit_price')::NUMERIC < 0 OR (p_el ->> 'unit_price')::NUMERIC >= 100000000)) THEN
        RAISE EXCEPTION 'unit_price must be a non-negative number';
    END IF;
    IF (p_el ? 'picked' AND jsonb_typeof(p_el -> 'picked') <> 'boolean')
       OR (p_el ? 'consolidated' AND jsonb_typeof(p_el -> 'consolidated') <> 'boolean') THEN
        RAISE EXCEPTION 'picked and consolidated must be true or false';
    END IF;
    v_d := btrim(COALESCE(COALESCE(p_el ->> 'description', p_el ->> 'name'), ''));
    IF char_length(v_d) > 500 OR char_length(COALESCE(p_el ->> 'lot_number', '')) > 200 THEN
        RAISE EXCEPTION 'description or lot_number too long';
    END IF;
    v_out := jsonb_build_object('quantity', (p_el ->> 'quantity')::INT, 'description', v_d,
                                'lot', NULLIF(btrim(COALESCE(p_el ->> 'lot_number', '')), ''));
    IF COALESCE(p_el ->> 'expiration_date', '') <> '' THEN
        IF (p_el ->> 'expiration_date') !~ '^\d{4}-\d{2}-\d{2}' THEN
            RAISE EXCEPTION 'expiration_date must be YYYY-MM-DD';
        END IF;
        BEGIN
            v_out := v_out || jsonb_build_object('exp', left(p_el ->> 'expiration_date', 10)::DATE::TEXT);
        EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN
            RAISE EXCEPTION 'expiration_date must be a real date';
        END;
    END IF;
    IF jsonb_typeof(p_el -> 'unit_price') = 'number' THEN
        v_out := v_out || jsonb_build_object('price', p_el -> 'unit_price');
    END IF;
    RETURN v_out;
END $$;

-- Replace the lines of an active shipment. p_lines: [{"item_id": "12", "quantity": 3,
-- "picked": false, "consolidated": false, "unit_price": 0.5}, ...] (flags/price optional: kept on existing
-- lines, false/NULL on new ones). A line without item_id is a free-text line
-- {"description": "Donated crutches", "quantity": 2, "unit_price": 10, "lot_number": "..", "expiration_date": ".."}:
-- no inventory, no allocation check, no stock taken. Inventory lines are matched by item, so their line
-- ids survive; free-text lines are all replaced (new ids). Raising a quantity (or adding an inventory
-- line) beyond what is available raises check_violation (clear message, nothing written).
-- Returns the number of lines.
CREATE OR REPLACE FUNCTION set_shipment_lines(p_id TEXT, p_lines JSONB) RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE
    v_stage TEXT;
    v_el    JSONB;
    v_f     JSONB;
    v_item  BIGINT;
    v_old   INT;
    v_keep  BIGINT[] := '{}';
    v_free  INT := 0;
BEGIN
    SELECT stage INTO v_stage FROM dash_shipment WHERE id = btrim(p_id) FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shipment % not found', p_id USING ERRCODE = 'no_data_found';
    END IF;
    IF v_stage NOT IN ('planned', 'pulling', 'consolidated') THEN
        RAISE EXCEPTION 'cannot change the lines of a % shipment', v_stage;
    END IF;
    IF p_lines IS NULL OR jsonb_typeof(p_lines) <> 'array' THEN
        RAISE EXCEPTION 'lines must be a JSON array';
    END IF;
    IF jsonb_array_length(p_lines) > 500 THEN
        RAISE EXCEPTION 'at most 500 lines';
    END IF;
    -- lock the items in a fixed order so two writers cannot deadlock
    PERFORM 1 FROM inventory_item
     WHERE item_id IN (SELECT (e ->> 'item_id')::BIGINT FROM jsonb_array_elements(p_lines) e
                        WHERE jsonb_typeof(e) = 'object' AND (e ->> 'item_id') ~ '^\d{1,18}$')
     ORDER BY item_id FOR UPDATE;
    DELETE FROM dash_shipment_line WHERE shipment_id = btrim(p_id) AND item_id IS NULL;
    FOR v_el IN SELECT * FROM jsonb_array_elements(p_lines) LOOP
        v_f := b4p_line_fields(v_el);
        IF jsonb_typeof(v_el -> 'item_id') IS NULL OR jsonb_typeof(v_el -> 'item_id') = 'null' THEN
            IF v_f ->> 'description' = '' THEN
                RAISE EXCEPTION 'a line needs an item_id or a description';
            END IF;
            INSERT INTO dash_shipment_line (shipment_id, description, lot_snapshot, exp_snapshot, unit_price, quantity,
                                            picked, consolidated, taken)
            VALUES (btrim(p_id), v_f ->> 'description', v_f ->> 'lot', (v_f ->> 'exp')::DATE, (v_f ->> 'price')::NUMERIC,
                    (v_f ->> 'quantity')::INT, COALESCE((v_el ->> 'picked')::BOOLEAN, false),
                    COALESCE((v_el ->> 'consolidated')::BOOLEAN, false), true);
            v_free := v_free + 1;
            CONTINUE;
        END IF;
        IF (v_el ->> 'item_id') !~ '^\d{1,18}$' THEN
            RAISE EXCEPTION 'item % not found', v_el ->> 'item_id' USING ERRCODE = 'no_data_found';
        END IF;
        v_item := (v_el ->> 'item_id')::BIGINT;
        IF NOT EXISTS (SELECT 1 FROM inventory_item WHERE item_id = v_item AND removed_at IS NULL) THEN
            RAISE EXCEPTION 'item % not found', v_item USING ERRCODE = 'no_data_found';
        END IF;
        IF v_item = ANY (v_keep) THEN
            RAISE EXCEPTION 'item % is listed twice', v_item;
        END IF;
        v_keep := v_keep || v_item;
        SELECT quantity INTO v_old FROM dash_shipment_line WHERE shipment_id = btrim(p_id) AND item_id = v_item;
        IF v_old IS NULL OR (v_f ->> 'quantity')::INT > v_old THEN
            PERFORM b4p_check_allocation(v_item, btrim(p_id), (v_f ->> 'quantity')::INT);
        END IF;
        INSERT INTO dash_shipment_line (shipment_id, item_id, quantity, unit_price, picked, consolidated)
        VALUES (btrim(p_id), v_item, (v_f ->> 'quantity')::INT, (v_f ->> 'price')::NUMERIC,
                COALESCE((v_el ->> 'picked')::BOOLEAN, false), COALESCE((v_el ->> 'consolidated')::BOOLEAN, false))
        ON CONFLICT (shipment_id, item_id) DO UPDATE
           SET quantity = EXCLUDED.quantity,
               unit_price = CASE WHEN v_el ? 'unit_price' THEN EXCLUDED.unit_price ELSE dash_shipment_line.unit_price END,
               picked = CASE WHEN v_el ? 'picked' THEN EXCLUDED.picked ELSE dash_shipment_line.picked END,
               consolidated = CASE WHEN v_el ? 'consolidated' THEN EXCLUDED.consolidated
                                   ELSE dash_shipment_line.consolidated END;
    END LOOP;
    DELETE FROM dash_shipment_line WHERE shipment_id = btrim(p_id) AND item_id IS NOT NULL AND NOT (item_id = ANY (v_keep));
    UPDATE dash_shipment SET updated_at = now() WHERE id = btrim(p_id);
    RETURN cardinality(v_keep) + v_free;
END $$;

-- Tick a line off. NULL = leave unchanged. Only lines of active shipments. Returns false if no such line.
CREATE OR REPLACE FUNCTION set_shipment_line_flags(p_line_id BIGINT, p_picked BOOLEAN, p_consolidated BOOLEAN)
RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE
    v_ship TEXT;
    v_stage TEXT;
BEGIN
    SELECT l.shipment_id, s.stage INTO v_ship, v_stage
      FROM dash_shipment_line l JOIN dash_shipment s ON s.id = l.shipment_id WHERE l.id = p_line_id FOR UPDATE OF s;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF v_stage NOT IN ('planned', 'pulling', 'consolidated') THEN
        RAISE EXCEPTION 'cannot change the lines of a % shipment', v_stage;
    END IF;
    UPDATE dash_shipment_line SET picked = COALESCE(p_picked, picked),
                                  consolidated = COALESCE(p_consolidated, consolidated)
     WHERE id = p_line_id;
    UPDATE dash_shipment SET updated_at = now() WHERE id = v_ship;
    RETURN true;
END $$;

-- Take the stock of a shipment out of inventory (once: lines flagged `taken`). Quantity is
-- kept like a dashboard edit (quantity_set_at) until someone edits the sheet's Quantity cell.
-- Free-text lines (no item) take nothing. Stock of a request this shipment carries (request_ids)
-- that was already taken when the request was marked Shipped before it was linked (shipment_pick
-- rows not yet claimed) is credited: per product name that many units are not taken again, and
-- those picks are marked claimed_by this shipment.
CREATE OR REPLACE FUNCTION b4p_ship_stock(p_id TEXT) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    v_line   RECORD;
    v_have   INT;
    v_reqs   TEXT[];
    v_cred   JSONB := '{}';
    v_key    TEXT;
    v_c      INT;
    v_skip   INT;
BEGIN
    SELECT request_ids INTO v_reqs FROM dash_shipment WHERE id = p_id;
    FOR v_line IN SELECT l.id, l.item_id, l.quantity, lower(btrim(i.raw_name)) AS k
                    FROM dash_shipment_line l JOIN inventory_item i ON i.item_id = l.item_id
                   WHERE l.shipment_id = p_id AND NOT l.taken ORDER BY l.item_id LOOP
        v_key := v_line.k;
        IF NOT v_cred ? v_key THEN
            WITH c AS (
                UPDATE shipment_pick pk SET claimed_by = p_id
                  FROM inventory_item pi
                 WHERE pk.item_id = pi.item_id AND pk.request_id = ANY (v_reqs) AND pk.claimed_by IS NULL
                   AND lower(btrim(pi.raw_name)) = v_key
             RETURNING pk.qty)
            SELECT COALESCE(sum(qty), 0) INTO v_c FROM c;
            v_cred := v_cred || jsonb_build_object(v_key, v_c);
        END IF;
        v_skip := LEAST((v_cred ->> v_key)::INT, v_line.quantity);
        v_cred := v_cred || jsonb_build_object(v_key, (v_cred ->> v_key)::INT - v_skip);
        SELECT quantity INTO v_have FROM inventory_item WHERE item_id = v_line.item_id FOR UPDATE;
        IF v_have < v_line.quantity - v_skip THEN
            RAISE EXCEPTION 'cannot ship: only % of item % on hand, shipment has %', v_have, v_line.item_id, v_line.quantity - v_skip
                USING ERRCODE = 'check_violation',
                      DETAIL = json_build_object('item_id', v_line.item_id::TEXT, 'requested', v_line.quantity - v_skip,
                                                 'available', v_have)::TEXT;
        END IF;
        UPDATE inventory_item SET quantity = quantity - (v_line.quantity - v_skip), quantity_set_at = clock_timestamp(),
                                  updated_at = now() WHERE item_id = v_line.item_id;
        UPDATE dash_shipment_line SET taken = true WHERE id = v_line.id;
    END LOOP;
END $$;

-- Create a shipment with its lines. p_header keys (all optional but name): name, recipient,
-- shipment_type, shipping_method, blueprints_contact, stage (default planned), ship_date,
-- delivered_date, request_ids, notes. p_lines as in set_shipment_lines.
-- p_id: optional (import); if a shipment with that id exists nothing is written and the id is returned.
-- p_import: for old localStorage shipments; a shipped/delivered one is stored as history
-- (lines flagged taken, inventory NOT touched), created_at may be given in p_header.
-- Returns the shipment id ('SHP-<epoch>-<3 digits>' when generated).
CREATE OR REPLACE FUNCTION create_shipment(p_id TEXT, p_header JSONB, p_lines JSONB, p_import BOOLEAN DEFAULT false)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    v_id    TEXT := NULLIF(btrim(COALESCE(p_id, '')), '');
    v_h     JSONB;
    v_stage TEXT;
    v_try   INT := 0;
    v_ct    TIMESTAMPTZ;
    v_el    JSONB;
    v_f     JSONB;
BEGIN
    v_h := b4p_shipment_header(COALESCE(p_header, '{}'));
    IF NOT (v_h ? 'name') THEN
        RAISE EXCEPTION 'name is required';
    END IF;
    v_stage := COALESCE(v_h ->> 'stage', 'planned');
    IF p_header ? 'created_at' AND p_import AND (p_header ->> 'created_at') ~ '^\d{4}-\d{2}-\d{2}' THEN
        BEGIN
            v_ct := (p_header ->> 'created_at')::TIMESTAMPTZ;
        EXCEPTION WHEN OTHERS THEN
            v_ct := NULL;
        END;
    END IF;
    IF v_stage IN ('shipped', 'delivered') AND NOT p_import THEN
        RAISE EXCEPTION 'create the shipment first, then move it to shipped';
    END IF;
    IF v_id IS NOT NULL THEN
        IF char_length(v_id) > 100 THEN RAISE EXCEPTION 'id too long'; END IF;
        PERFORM pg_advisory_xact_lock(hashtext('dash_shipment:' || v_id));
        IF EXISTS (SELECT 1 FROM dash_shipment WHERE id = v_id) THEN
            RETURN v_id;
        END IF;
    ELSE
        LOOP
            v_id := 'SHP-' || extract(epoch FROM now())::BIGINT || '-' || lpad((floor(random() * 1000))::INT::TEXT, 3, '0');
            EXIT WHEN NOT EXISTS (SELECT 1 FROM dash_shipment WHERE id = v_id);
            v_try := v_try + 1;
            IF v_try > 50 THEN RAISE EXCEPTION 'could not generate a shipment id'; END IF;
        END LOOP;
    END IF;
    -- created active; moved to its final stage after the lines are checked (history skips the checks)
    INSERT INTO dash_shipment (id, name, recipient, shipment_type, shipping_method, blueprints_contact, stage,
                               ship_date, delivered_date, request_ids, notes, kind, unit_count, value, created_at, updated_at)
    VALUES (v_id, v_h ->> 'name', COALESCE(v_h ->> 'recipient', ''), COALESCE(v_h ->> 'shipment_type', ''),
            COALESCE(v_h ->> 'shipping_method', ''), COALESCE(v_h ->> 'blueprints_contact', ''),
            CASE WHEN v_stage IN ('shipped', 'delivered') THEN 'planned' ELSE v_stage END,
            (v_h ->> 'ship_date')::DATE, (v_h ->> 'delivered_date')::DATE,
            COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_h -> 'request_ids')), '{}'),
            COALESCE(v_h ->> 'notes', ''), COALESCE(v_h ->> 'kind', 'inventory'), (v_h ->> 'unit_count')::INT,
            (v_h ->> 'value')::NUMERIC, COALESCE(v_ct, now()), COALESCE(v_ct, now()));
    IF p_lines IS NOT NULL AND jsonb_typeof(p_lines) = 'array' AND jsonb_array_length(p_lines) > 0 THEN
        IF v_stage IN ('shipped', 'delivered') THEN
            -- history: no stock checks, no stock taken. A line whose item no longer exists becomes a
            -- free-text line (name / lot / expiration / price as given) so it still displays.
            FOR v_el IN SELECT * FROM jsonb_array_elements(p_lines) LOOP
                v_f := b4p_line_fields(v_el);
                IF COALESCE(v_el ->> 'item_id', '') ~ '^\d{1,18}$'
                   AND EXISTS (SELECT 1 FROM inventory_item WHERE item_id = (v_el ->> 'item_id')::BIGINT) THEN
                    INSERT INTO dash_shipment_line (shipment_id, item_id, description, lot_snapshot, exp_snapshot, unit_price,
                                                    quantity, picked, consolidated, taken)
                    VALUES (v_id, (v_el ->> 'item_id')::BIGINT, v_f ->> 'description', v_f ->> 'lot', (v_f ->> 'exp')::DATE,
                            (v_f ->> 'price')::NUMERIC, (v_f ->> 'quantity')::INT, true, true, true)
                    ON CONFLICT (shipment_id, item_id) DO NOTHING;
                ELSIF v_f ->> 'description' <> '' THEN
                    INSERT INTO dash_shipment_line (shipment_id, description, lot_snapshot, exp_snapshot, unit_price,
                                                    quantity, picked, consolidated, taken)
                    VALUES (v_id, v_f ->> 'description', v_f ->> 'lot', (v_f ->> 'exp')::DATE, (v_f ->> 'price')::NUMERIC,
                            (v_f ->> 'quantity')::INT, true, true, true);
                END IF;
            END LOOP;
        ELSE
            PERFORM set_shipment_lines(v_id, p_lines);
        END IF;
    ELSIF p_lines IS NOT NULL AND jsonb_typeof(p_lines) <> 'array' AND jsonb_typeof(p_lines) <> 'null' THEN
        RAISE EXCEPTION 'lines must be a JSON array';
    END IF;
    IF v_stage IN ('shipped', 'delivered') THEN
        UPDATE dash_shipment SET stage = v_stage,
               ship_date = COALESCE(ship_date, created_at::DATE),
               delivered_date = CASE WHEN v_stage = 'delivered' THEN COALESCE(delivered_date, ship_date, created_at::DATE)
                                     ELSE delivered_date END
         WHERE id = v_id;
    END IF;
    RETURN v_id;
END $$;

-- Change header fields and/or stage. p_patch: any of the header keys (see create_shipment);
-- only the keys present change (null clears an optional one). Stage moves: forward (steps may
-- be skipped), or back exactly one step among planned/pulling/consolidated; nothing leaves shipped/delivered
-- backward. Reaching shipped takes the stock out of inventory (over-stock raises check_violation);
-- ship_date defaults to today then, delivered_date to today on delivered.
-- Returns false if the shipment does not exist.
CREATE OR REPLACE FUNCTION update_shipment(p_id TEXT, p_patch JSONB) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    v_h   JSONB;
    v_cur dash_shipment%ROWTYPE;
    v_new TEXT;
BEGIN
    v_h := b4p_shipment_header(COALESCE(p_patch, '{}'));
    SELECT * INTO v_cur FROM dash_shipment WHERE id = btrim(p_id) FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    v_new := COALESCE(v_h ->> 'stage', v_cur.stage);
    IF v_new <> v_cur.stage THEN
        IF NOT (b4p_stage_rank(v_new) > b4p_stage_rank(v_cur.stage)
                OR (b4p_stage_rank(v_cur.stage) <= 3 AND b4p_stage_rank(v_new) = b4p_stage_rank(v_cur.stage) - 1)) THEN
            RAISE EXCEPTION 'cannot change shipment % from % to %', v_cur.id, v_cur.stage, v_new;
        END IF;
    END IF;
    UPDATE dash_shipment SET
        name = COALESCE(v_h ->> 'name', name),
        recipient = COALESCE(v_h ->> 'recipient', recipient),
        shipment_type = COALESCE(v_h ->> 'shipment_type', shipment_type),
        shipping_method = COALESCE(v_h ->> 'shipping_method', shipping_method),
        blueprints_contact = COALESCE(v_h ->> 'blueprints_contact', blueprints_contact),
        notes = COALESCE(v_h ->> 'notes', notes),
        kind = COALESCE(v_h ->> 'kind', kind),
        unit_count = CASE WHEN v_h ? 'unit_count' THEN (v_h ->> 'unit_count')::INT ELSE unit_count END,
        value = CASE WHEN v_h ? 'value' THEN (v_h ->> 'value')::NUMERIC ELSE value END,
        ship_date = CASE WHEN v_h ? 'ship_date' THEN (v_h ->> 'ship_date')::DATE ELSE ship_date END,
        delivered_date = CASE WHEN v_h ? 'delivered_date' THEN (v_h ->> 'delivered_date')::DATE ELSE delivered_date END,
        request_ids = CASE WHEN v_h ? 'request_ids'
                           THEN COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_h -> 'request_ids')), '{}')
                           ELSE request_ids END,
        stage = v_new,
        updated_at = now()
     WHERE id = v_cur.id;
    IF b4p_stage_rank(v_new) >= 4 AND b4p_stage_rank(v_cur.stage) < 4 THEN
        PERFORM b4p_ship_stock(v_cur.id);
        UPDATE dash_shipment SET ship_date = COALESCE(ship_date, CURRENT_DATE) WHERE id = v_cur.id;
    END IF;
    IF v_new = 'delivered' THEN
        UPDATE dash_shipment SET delivered_date = COALESCE(delivered_date, CURRENT_DATE) WHERE id = v_cur.id;
    END IF;
    RETURN true;
END $$;

COMMIT;
