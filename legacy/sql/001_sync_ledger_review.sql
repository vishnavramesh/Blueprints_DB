-- 001 — stable sync keys, ledger-backed quantities, audit log, review queue,
-- fulfillment, normalization rules in tables, sync bookkeeping.
-- Apply on top of schema.sql:   psql -d b4p -v ON_ERROR_STOP=1 -f migrations/001_sync_ledger_review.sql
-- Idempotence is NOT attempted: run each migration exactly once (tracked in
-- schema_migration below).

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migration (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migration (version) VALUES ('001');   -- fails loudly if re-run

-- ===========================================================================
-- 1. Stable row identity for the feed
--    source_key = the row's Image URL (unique per scan); source_hash becomes a
--    change detector instead of the identity, so an edited row is UPDATEd in
--    place rather than re-inserted as a duplicate.
-- ===========================================================================
ALTER TABLE inventory_item
    ADD COLUMN source_key  TEXT,
    ADD COLUMN removed_at  TIMESTAMPTZ,            -- vanished from the feed; kept for history
    ADD COLUMN reviewed_at TIMESTAMPTZ;            -- a human resolved the flag; sync won't undo it

UPDATE inventory_item
   SET source_key = COALESCE(image_url, 'hash:' || source_hash)
 WHERE source_hash IS NOT NULL;

ALTER TABLE inventory_item DROP CONSTRAINT inventory_item_source_hash_key;
CREATE UNIQUE INDEX inventory_item_source_key ON inventory_item(source_key);
CREATE INDEX inventory_item_active ON inventory_item(item_id) WHERE removed_at IS NULL;

-- ===========================================================================
-- 2. Unreadable AI scans don't belong in the catalog.
--    Flagged '{...' rows keep their raw_name; the junk supply rows go away.
-- ===========================================================================
UPDATE inventory_item i SET supply_id = NULL
  FROM supply s
 WHERE s.supply_id = i.supply_id AND s.name LIKE '{%' AND i.flagged AND i.raw_name IS NOT NULL;
DELETE FROM supply s
 WHERE s.name LIKE '{%'
   AND NOT EXISTS (SELECT 1 FROM inventory_item i WHERE i.supply_id = s.supply_id);

-- ===========================================================================
-- 3. Ledger: every quantity change is an inventory_transaction.
--    Backfill one 'received' row per existing lot with the trigger disabled
--    (the quantities are already there; we only need the history row).
-- ===========================================================================
ALTER TABLE inventory_transaction ADD COLUMN source TEXT;   -- 'sync', 'api', 'load', 'migration'

ALTER TABLE inventory_transaction DISABLE TRIGGER trg_apply_txn;
INSERT INTO inventory_transaction (item_id, txn_type, delta, note, source, created_at)
SELECT item_id, 'received', quantity, 'opening balance (migration 001)', 'migration',
       warehouse_date::timestamptz
  FROM inventory_item WHERE quantity > 0;
ALTER TABLE inventory_transaction ENABLE TRIGGER trg_apply_txn;

-- ===========================================================================
-- 4. Audit log: who/what changed which row, old and new values.
--    Writers tag themselves with  SET LOCAL b4p.source = 'sync'  (or 'api').
-- ===========================================================================
CREATE TABLE audit_log (
    audit_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name TEXT NOT NULL,
    row_id     BIGINT,
    op         TEXT NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
    old_row    JSONB,
    new_row    JSONB,
    source     TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_row ON audit_log(table_name, row_id);

CREATE FUNCTION audit_row() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pk  TEXT := TG_ARGV[0];
    o   JSONB := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
    n   JSONB := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
BEGIN
    IF TG_OP = 'UPDATE' AND o = n THEN RETURN NEW; END IF;   -- no-op upserts
    INSERT INTO audit_log (table_name, row_id, op, old_row, new_row, source)
    VALUES (TG_TABLE_NAME, (COALESCE(n, o) ->> pk)::BIGINT, TG_OP, o, n,
            NULLIF(current_setting('b4p.source', true), ''));
    RETURN COALESCE(NEW, OLD);
END $$;

CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON inventory_item
    FOR EACH ROW EXECUTE FUNCTION audit_row('item_id');
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON supply
    FOR EACH ROW EXECUTE FUNCTION audit_row('supply_id');
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON shipment
    FOR EACH ROW EXECUTE FUNCTION audit_row('shipment_id');
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON box
    FOR EACH ROW EXECUTE FUNCTION audit_row('box_id');

-- ===========================================================================
-- 5. Unit values: per-category fallback so total_value isn't always 0.
--    Fill these in with real estimates:  UPDATE category SET default_unit_value_usd = ...
-- ===========================================================================
ALTER TABLE category ADD COLUMN default_unit_value_usd NUMERIC(10,2)
    CHECK (default_unit_value_usd >= 0);

-- ===========================================================================
-- 6. Normalization rules as data (edit rows, not code).
--    The service loads these at the start of every sync cycle.
-- ===========================================================================
CREATE TABLE category_alias (
    alias       CITEXT PRIMARY KEY,
    category_id BIGINT NOT NULL REFERENCES category(category_id) ON DELETE CASCADE
);

CREATE TABLE box_type_alias (
    alias CITEXT PRIMARY KEY,
    code  CITEXT NOT NULL REFERENCES box_type(code) ON UPDATE CASCADE ON DELETE CASCADE
);
INSERT INTO box_type_alias (alias, code) VALUES
    ('STN', 'ST/N'), ('TCRC', 'TC/RC'), ('TC/RC/', 'TC/RC'), ('TCRC/', 'TC/RC');

CREATE TABLE status_alias (
    alias  CITEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('requested','under_review','approved','packing','shipped','delivered','cancelled'))
);
INSERT INTO status_alias (alias, status) VALUES
    ('under review', 'under_review'), ('approved', 'approved'), ('shipped', 'shipped'),
    ('delivered', 'delivered'), ('cancelled', 'cancelled'), ('denied', 'cancelled'),
    ('packing', 'packing'), ('requested', 'requested');

-- manufacturer_alias already exists in schema.sql; seed it from load.py's map.
INSERT INTO manufacturer (canonical_name)
VALUES ('BD'), ('Cardinal Health'), ('Medline'), ('Owens & Minor')
ON CONFLICT (canonical_name) DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id)
SELECT a.alias, m.manufacturer_id
  FROM (VALUES ('bd','BD'), ('becton, dickinson and company','BD'), ('becton dickinson','BD'),
               ('cardinalhealth','Cardinal Health'), ('cardinal health 200, llc','Cardinal Health'),
               ('medline industries','Medline'), ('medline industries, lp','Medline'),
               ('medline','Medline'), ('owens & minor, inc.','Owens & Minor')) AS a(alias, canon)
  JOIN manufacturer m ON m.canonical_name = a.canon
ON CONFLICT (alias) DO NOTHING;

-- ===========================================================================
-- 7. Request line items get a stable position so multi-line requests sync
--    every line (the old sync only ever inserted the first one).
-- ===========================================================================
ALTER TABLE shipment_item
    ADD COLUMN line_no   INTEGER,
    ADD COLUMN supply_id BIGINT REFERENCES supply(supply_id) ON DELETE SET NULL;  -- catalog match
UPDATE shipment_item si SET line_no = x.n
  FROM (SELECT shipment_item_id,
               row_number() OVER (PARTITION BY shipment_id ORDER BY shipment_item_id) AS n
          FROM shipment_item) x
 WHERE x.shipment_item_id = si.shipment_item_id;
ALTER TABLE shipment_item ALTER COLUMN line_no SET NOT NULL;
ALTER TABLE shipment_item ADD CONSTRAINT shipment_item_line UNIQUE (shipment_id, line_no);

-- ===========================================================================
-- 8. Sync bookkeeping (backs GET /sync/status)
-- ===========================================================================
CREATE TABLE sync_run (
    run_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    items_added    INTEGER NOT NULL DEFAULT 0,
    items_updated  INTEGER NOT NULL DEFAULT 0,
    items_removed  INTEGER NOT NULL DEFAULT 0,
    rows_skipped   INTEGER NOT NULL DEFAULT 0,
    requests_added INTEGER NOT NULL DEFAULT 0,
    lines_added    INTEGER NOT NULL DEFAULT 0,
    status_changes INTEGER NOT NULL DEFAULT 0,
    supplies_feed_ok BOOLEAN,
    requests_feed_ok BOOLEAN,
    warnings       JSONB,
    error          TEXT
);

-- ===========================================================================
-- 9. Views (recreated to hide removed rows and add value fallbacks)
-- ===========================================================================
DROP VIEW v_kpis;
DROP VIEW v_inventory;

CREATE VIEW v_inventory AS
SELECT i.item_id,
       COALESCE(s.name, i.raw_name) AS name, s.general_name,
       m.canonical_name AS manufacturer,
       c.name AS category,
       i.quantity, i.lot_number, i.expiration_date,
       (i.expiration_date IS NOT NULL AND i.expiration_date < CURRENT_DATE) AS is_expired,
       i.warehouse_date AS date_inventoried,
       CURRENT_DATE - i.warehouse_date AS days_in_warehouse,
       b.box_type || '-' || b.box_number AS box_label,
       b.pallet_number,
       i.reserved_for AS shipment_id,
       r.org_name AS reserved_for_org,
       i.flagged, i.raw_name, i.image_url,
       s.category_id, i.box_id, i.supply_id
FROM inventory_item i
LEFT JOIN shipment sh     ON sh.shipment_id = i.reserved_for
LEFT JOIN requester r     ON r.requester_id = sh.requester_id
LEFT JOIN supply s        ON s.supply_id = i.supply_id
LEFT JOIN manufacturer m  ON m.manufacturer_id = s.manufacturer_id
LEFT JOIN category c      ON c.category_id = s.category_id
LEFT JOIN box b           ON b.box_id = i.box_id
WHERE i.removed_at IS NULL;

CREATE VIEW v_kpis AS
SELECT COUNT(*)                                   AS total_entries,
       COALESCE(SUM(i.quantity),0)                AS total_raw_items,
       COALESCE(SUM(i.quantity * COALESCE(s.unit_value_usd, c.default_unit_value_usd)),0) AS total_value,
       COUNT(*) FILTER (WHERE s.unit_value_usd IS NULL AND c.default_unit_value_usd IS NULL)
                                                  AS entries_without_value,
       COUNT(DISTINCT i.box_id)                   AS box_count
FROM inventory_item i
LEFT JOIN supply s   USING (supply_id)
LEFT JOIN category c ON c.category_id = s.category_id
WHERE i.removed_at IS NULL;

-- expired / expiring soon, for alerts
CREATE VIEW v_expiring AS
SELECT v.*,
       v.expiration_date - CURRENT_DATE AS days_left,
       CASE WHEN v.expiration_date <  CURRENT_DATE      THEN 'expired'
            WHEN v.expiration_date <  CURRENT_DATE + 30 THEN '30d'
            ELSE '90d' END AS bucket
FROM v_inventory v
WHERE v.expiration_date IS NOT NULL
  AND v.expiration_date < CURRENT_DATE + 90
  AND v.quantity > 0;

-- pick list for a shipment: what to pull, where from
CREATE VIEW v_picklist AS
SELECT sh.shipment_id, sh.request_id, r.org_name,
       v.pallet_number, v.box_label, v.item_id, v.name, v.manufacturer,
       v.lot_number, v.expiration_date, v.quantity
FROM shipment sh
JOIN requester r   ON r.requester_id = sh.requester_id
JOIN v_inventory v ON v.shipment_id = sh.shipment_id;

-- flagged scans awaiting a human, with the text to match on
-- ('{surgical scissors}' is the AI's guess wrapped in braces)
CREATE VIEW v_review_queue AS
SELECT i.item_id, i.raw_name, btrim(COALESCE(i.raw_name, s.name), '{} ') AS match_text,
       s.name AS current_supply, i.quantity, i.lot_number, i.expiration_date,
       b.box_type || '-' || b.box_number AS box_label, i.image_url, i.notes
FROM inventory_item i
LEFT JOIN supply s ON s.supply_id = i.supply_id
LEFT JOIN box b    ON b.box_id = i.box_id
WHERE i.flagged AND i.removed_at IS NULL;

-- ===========================================================================
-- 10. Catalog matching (pg_trgm)
-- ===========================================================================
-- the numbers in a product name ('BD 10ml Syringe 23G' -> '10 23'): names
-- that differ only in size/gauge look alike to trigrams but are different
-- products, so matching treats a numbers mismatch as a strong negative.
CREATE FUNCTION name_numbers(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT COALESCE(string_agg(m[1], ' '), '')
      FROM regexp_matches(t, '(\d+(?:\.\d+)?)', 'g') AS m
$$;

-- closest catalog products for free text, best first (same-numbers first)
CREATE FUNCTION suggest_supplies(q TEXT, n INTEGER DEFAULT 3)
RETURNS TABLE (supply_id BIGINT, name TEXT, manufacturer TEXT, score REAL)
LANGUAGE sql STABLE AS $$
    SELECT s.supply_id, s.name, m.canonical_name::TEXT, similarity(s.name, q)
      FROM supply s LEFT JOIN manufacturer m USING (manufacturer_id)
     WHERE s.name % q
     ORDER BY name_numbers(s.name) = name_numbers(q) DESC,
              similarity(s.name, q) DESC, s.supply_id
     LIMIT n
$$;

-- likely-duplicate catalog entries (same or near-same name)
CREATE VIEW v_duplicate_supplies AS
SELECT a.supply_id AS supply_a, a.name AS name_a, ma.canonical_name AS mfr_a,
       b.supply_id AS supply_b, b.name AS name_b, mb.canonical_name AS mfr_b,
       similarity(a.name, b.name) AS score
FROM supply a
JOIN supply b ON a.supply_id < b.supply_id AND a.name % b.name
LEFT JOIN manufacturer ma ON ma.manufacturer_id = a.manufacturer_id
LEFT JOIN manufacturer mb ON mb.manufacturer_id = b.manufacturer_id
WHERE similarity(a.name, b.name) >= 0.8
  AND name_numbers(a.name) = name_numbers(b.name);

-- fold supply `dup` into `keep`: repoint stock and request lines, then delete.
CREATE FUNCTION merge_supply(keep BIGINT, dup BIGINT) RETURNS INTEGER
LANGUAGE plpgsql AS $$
DECLARE moved INTEGER;
BEGIN
    IF keep = dup THEN RAISE EXCEPTION 'cannot merge a supply into itself'; END IF;
    PERFORM 1 FROM supply WHERE supply_id = keep;
    IF NOT FOUND THEN RAISE EXCEPTION 'supply % not found', keep; END IF;
    UPDATE inventory_item SET supply_id = keep WHERE supply_id = dup;
    GET DIAGNOSTICS moved = ROW_COUNT;
    UPDATE shipment_item SET supply_id = keep WHERE supply_id = dup;
    UPDATE supply k SET general_name   = COALESCE(k.general_name, d.general_name),
                        ref_number     = COALESCE(k.ref_number, d.ref_number),
                        unit_value_usd = COALESCE(k.unit_value_usd, d.unit_value_usd)
      FROM supply d WHERE k.supply_id = keep AND d.supply_id = dup;
    DELETE FROM supply WHERE supply_id = dup;
    IF NOT FOUND THEN RAISE EXCEPTION 'supply % not found', dup; END IF;
    RETURN moved;
END $$;

-- ===========================================================================
-- 11. Fulfillment: reserve stock for a shipment's line items.
--     Each line is matched to its best catalog product (trigram), then whole
--     lots are reserved first-expiring-first until the requested quantity is
--     covered. Expired, flagged, removed and already-reserved lots are skipped.
--     Lots are not split: a lot bigger than the remainder is reserved whole.
-- ===========================================================================
CREATE FUNCTION reserve_shipment(p_shipment BIGINT, min_score REAL DEFAULT 0.3)
RETURNS TABLE (shipment_item_id BIGINT, item_name TEXT, matched_supply BIGINT,
               item_id BIGINT, quantity INTEGER)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    line RECORD;
    lot  RECORD;
    need INTEGER;
BEGIN
    PERFORM 1 FROM shipment WHERE shipment_id = p_shipment
        AND status NOT IN ('shipped','delivered','cancelled');
    IF NOT FOUND THEN RAISE EXCEPTION 'shipment % not found or closed', p_shipment; END IF;

    FOR line IN SELECT * FROM shipment_item si WHERE si.shipment_id = p_shipment ORDER BY si.line_no LOOP
        IF line.supply_id IS NULL THEN
            SELECT g.supply_id INTO line.supply_id FROM suggest_supplies(line.item_name, 1) g
             WHERE g.score >= min_score;
            UPDATE shipment_item SET supply_id = line.supply_id
             WHERE shipment_item.shipment_item_id = line.shipment_item_id;
        END IF;
        CONTINUE WHEN line.supply_id IS NULL;

        -- already-reserved stock for this line's product counts toward the need
        SELECT COALESCE(line.quantity_requested, 1) - COALESCE(SUM(i.quantity), 0) INTO need
          FROM inventory_item i
         WHERE i.reserved_for = p_shipment AND i.supply_id = line.supply_id;

        FOR lot IN
            SELECT i.item_id, i.quantity FROM inventory_item i
             WHERE i.supply_id = line.supply_id AND i.reserved_for IS NULL
               AND i.removed_at IS NULL AND NOT i.flagged AND i.quantity > 0
               AND (i.expiration_date IS NULL OR i.expiration_date >= CURRENT_DATE)
             ORDER BY i.expiration_date NULLS LAST, i.item_id
             FOR UPDATE
        LOOP
            EXIT WHEN need <= 0;
            UPDATE inventory_item SET reserved_for = p_shipment WHERE inventory_item.item_id = lot.item_id;
            need := need - lot.quantity;
            shipment_item_id := line.shipment_item_id; item_name := line.item_name;
            matched_supply := line.supply_id; item_id := lot.item_id; quantity := lot.quantity;
            RETURN NEXT;
        END LOOP;
    END LOOP;
END $$;

CREATE FUNCTION release_shipment(p_shipment BIGINT) RETURNS INTEGER
LANGUAGE sql AS $$
    WITH r AS (UPDATE inventory_item SET reserved_for = NULL
                WHERE reserved_for = p_shipment RETURNING 1)
    SELECT count(*)::INTEGER FROM r
$$;

COMMIT;
