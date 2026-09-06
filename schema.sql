-- B4P Medical Supply Inventory — simplified PostgreSQL schema
-- Postgres 16+.  psql -d b4p -f schema.sql
--
-- Scope: supply catalog + physical inventory + boxes. No volunteers, no events.
-- Column names line up with the live dashboard payloads
-- (b4p-quantitative /items and b4p-external-view-w26-backend /api/supplies)
-- so imports are a straight field mapping.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy matching for the AI app
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------------------
-- Categories (e.g. 'Airway & Oxygen', 'Wound Care & Bandages')
-- parent_id lets a category have subcategories; NULL = top level.
-- ---------------------------------------------------------------------------
CREATE TABLE category (
    category_id BIGINT PRIMARY KEY,               -- fixed, standardized ids (seeded below)
    parent_id   BIGINT REFERENCES category(category_id) ON DELETE RESTRICT,
    name        CITEXT NOT NULL,
    UNIQUE NULLS NOT DISTINCT (parent_id, name)
);

-- Standardized category ids, seeded from the live dashboard data.
-- These ids are stable: use them everywhere (AI app, dashboards, imports).
INSERT INTO category (category_id, parent_id, name) VALUES
    ( 1, NULL, 'IV Supplies'),
    ( 2, NULL, 'Misc Surgical'),
    ( 3, NULL, 'Wound Care & Bandages'),
    ( 4, NULL, 'Needles & Syringes'),
    ( 5, NULL, 'Catheters & Briefs'),
    ( 6, NULL, 'Airway & Oxygen'),
    ( 7, NULL, 'Vitals & Home Health'),
    ( 8, NULL, 'Gloves'),
    ( 9, NULL, 'Apparel (Gowns, Shoe Covers, etc.)'),
    (10, NULL, 'Masks & Face Shields'),
    (11, NULL, 'Sutures'),
    (12, NULL, 'Misc Non-Medical'),
    (13, NULL, 'Medical Supplies'),               -- generic bucket seen in live data
    (99, NULL, 'Unspecified');                    -- catch-all for unclassified scans

CREATE TABLE manufacturer (
    manufacturer_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name  CITEXT NOT NULL UNIQUE
);

-- 'BD' / 'Becton, Dickinson and Company' -> one manufacturer row
CREATE TABLE manufacturer_alias (
    alias           CITEXT PRIMARY KEY,
    manufacturer_id BIGINT NOT NULL REFERENCES manufacturer(manufacturer_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Boxes: labeled <type>-<number>, e.g. ER-1, MISC-2, TC/RC-11
-- Types seeded from what actually appears in the live external dashboard data.
-- ---------------------------------------------------------------------------
CREATE TABLE box_type (
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
    ('LEGACY',  'Pre-convention boxes labeled by number only');

CREATE TABLE box (
    box_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    box_type      CITEXT  NOT NULL REFERENCES box_type(code) ON UPDATE CASCADE,
    box_number    INTEGER NOT NULL CHECK (box_number > 0),
    pallet_number INTEGER,
    UNIQUE (box_type, box_number)
);

-- printable label, e.g. 'ER-1'
CREATE VIEW v_box_label AS
SELECT box_id, box_type || '-' || box_number AS label, pallet_number FROM box;

-- ---------------------------------------------------------------------------
-- Requesters & shipments
-- requester = the org asking for supplies (Org Name / Org Email on the
-- external dashboard). shipment = one outgoing consignment, current or past.
-- ---------------------------------------------------------------------------
CREATE TABLE requester (
    requester_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_name     CITEXT NOT NULL UNIQUE,
    org_email    CITEXT
);

CREATE TABLE shipment (
    shipment_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requester_id BIGINT REFERENCES requester(requester_id) ON DELETE RESTRICT,
    request_id   TEXT UNIQUE,                     -- external dashboard 'REQ-...' id
    status       TEXT NOT NULL DEFAULT 'requested'
                 CHECK (status IN ('requested','under_review','approved','packing','shipped','delivered','cancelled')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    shipped_at   TIMESTAMPTZ,
    notes        TEXT
);
CREATE INDEX shipment_requester ON shipment(requester_id);

-- line items of a request: what was asked for (free text from the dashboard,
-- matched to the catalog later when reserving actual stock)
CREATE TABLE shipment_item (
    shipment_item_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shipment_id      BIGINT NOT NULL REFERENCES shipment(shipment_id) ON DELETE CASCADE,
    item_name        TEXT NOT NULL,
    category_id      BIGINT REFERENCES category(category_id),
    quantity_requested INTEGER CHECK (quantity_requested > 0)
);

-- ---------------------------------------------------------------------------
-- Supply catalog: one row per PRODUCT (what a thing is)
-- ---------------------------------------------------------------------------
CREATE TABLE supply (
    supply_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    manufacturer_id BIGINT REFERENCES manufacturer(manufacturer_id) ON DELETE RESTRICT,
    category_id     BIGINT NOT NULL REFERENCES category(category_id) ON DELETE RESTRICT,
    general_name    TEXT,                          -- 'wound dressing' ("General"/"gen_name" in dashboards)
    name            TEXT NOT NULL,                 -- full product name off the label
    ref_number      TEXT,                          -- REF if printed
    unit_value_usd  NUMERIC(10,2) CHECK (unit_value_usd >= 0),
    UNIQUE NULLS NOT DISTINCT (manufacturer_id, name)
);
-- a REF, when present, is unique per manufacturer
CREATE UNIQUE INDEX supply_mfr_ref ON supply(manufacturer_id, ref_number)
    WHERE ref_number IS NOT NULL;

CREATE INDEX supply_name_trgm ON supply USING gin (name gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Inventory: one row per physical LOT of a supply (what we actually have)
-- ---------------------------------------------------------------------------
CREATE TABLE inventory_item (
    item_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supply_id       BIGINT REFERENCES supply(supply_id) ON DELETE RESTRICT,
    box_id          BIGINT REFERENCES box(box_id) ON DELETE SET NULL,
    quantity        INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0 AND quantity < 1000000),
    lot_number      TEXT,
    expiration_date DATE,                          -- NULL = none printed = counts as unexpired
    warehouse_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    -- AI-scan fields: unmatched scans are flagged for review instead of
    -- polluting the supply catalog
    flagged         BOOLEAN NOT NULL DEFAULT false,
    raw_name        TEXT,
    image_url       TEXT,
    notes           TEXT,
    -- reservation: which shipment this stock is declared for; NULL = free
    reserved_for    BIGINT REFERENCES shipment(shipment_id) ON DELETE SET NULL,
    CHECK (supply_id IS NOT NULL OR (flagged AND raw_name IS NOT NULL))
);

CREATE INDEX inventory_item_supply  ON inventory_item(supply_id);
CREATE INDEX inventory_item_box     ON inventory_item(box_id);
CREATE INDEX inventory_item_flagged ON inventory_item(item_id) WHERE flagged;

-- ---------------------------------------------------------------------------
-- Stock movements (ships/donations recorded by the external dashboard).
-- Optional but recommended: quantity above is "current"; this is the history
-- and the natural sync point for dashboard request fulfillment.
-- ---------------------------------------------------------------------------
CREATE TABLE inventory_transaction (
    txn_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_id    BIGINT NOT NULL REFERENCES inventory_item(item_id) ON DELETE RESTRICT,
    txn_type   TEXT NOT NULL CHECK (txn_type IN ('received','shipped','donated','adjusted','disposed')),
    delta      INTEGER NOT NULL CHECK (delta <> 0),
    shipment_id BIGINT REFERENCES shipment(shipment_id) ON DELETE SET NULL, -- set on 'shipped' rows
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX inventory_transaction_item ON inventory_transaction(item_id);

-- keep inventory_item.quantity in step with the ledger automatically
CREATE FUNCTION apply_txn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE inventory_item SET quantity = quantity + NEW.delta WHERE item_id = NEW.item_id;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_apply_txn AFTER INSERT ON inventory_transaction
FOR EACH ROW EXECUTE FUNCTION apply_txn();

-- ---------------------------------------------------------------------------
-- Convenience views mirroring the dashboards
-- ---------------------------------------------------------------------------
CREATE VIEW v_inventory AS
SELECT i.item_id,
       s.name, s.general_name,
       m.canonical_name AS manufacturer,
       c.name AS category,
       i.quantity, i.lot_number, i.expiration_date,
       (i.expiration_date IS NOT NULL AND i.expiration_date < CURRENT_DATE) AS is_expired,
       CURRENT_DATE - i.warehouse_date AS days_in_warehouse,
       b.box_type || '-' || b.box_number AS box_label,
       b.pallet_number,
       i.reserved_for AS shipment_id,
       r.org_name AS reserved_for_org,
       i.flagged, i.raw_name, i.image_url
FROM inventory_item i
LEFT JOIN shipment sh     ON sh.shipment_id = i.reserved_for
LEFT JOIN requester r     ON r.requester_id = sh.requester_id
LEFT JOIN supply s        ON s.supply_id = i.supply_id
LEFT JOIN manufacturer m  ON m.manufacturer_id = s.manufacturer_id
LEFT JOIN category c      ON c.category_id = s.category_id
LEFT JOIN box b           ON b.box_id = i.box_id;

-- the /kpis payload of the internal dashboard, computed live
CREATE VIEW v_kpis AS
SELECT COUNT(*)                                   AS total_entries,
       COALESCE(SUM(quantity),0)                  AS total_raw_items,
       COALESCE(SUM(quantity * s.unit_value_usd),0) AS total_value,
       COUNT(DISTINCT i.box_id)                   AS box_count
FROM inventory_item i LEFT JOIN supply s USING (supply_id);

COMMIT;
