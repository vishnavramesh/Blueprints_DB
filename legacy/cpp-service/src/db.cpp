#include "db.h"
#include <spdlog/spdlog.h>

Db::Db(const std::string& conninfo, size_t pool_size)
    : conninfo_(conninfo), size_(pool_size ? pool_size : 1) {
    idle_.push_back(open());  // fail fast at startup if the DB is unreachable
    created_ = 1;
}

Db::ConnPtr Db::open() {
    spdlog::info("connecting to postgres: {}", conninfo_);
    auto c = std::make_unique<pqxx::connection>(conninfo_);
    prepare_all(*c);
    return c;
}

Db::Lease::Lease(Db& d) : db(d) {
    std::unique_lock<std::mutex> lock(db.mu_);
    db.cv_.wait(lock, [&] { return !db.idle_.empty() || db.created_ < db.size_; });
    if (!db.idle_.empty()) {
        conn = std::move(db.idle_.back());
        db.idle_.pop_back();
    } else {
        db.created_++;  // reserve the slot, then connect outside the lock
    }
    lock.unlock();
    try {
        // A connection whose server went away (brew services restart
        // postgresql@16) reports !is_open(); reconnect lazily so the service
        // survives a Postgres restart without needing its own.
        if (!conn || !conn->is_open()) conn = db.open();
    } catch (...) {
        std::lock_guard<std::mutex> g(db.mu_);
        db.created_--;
        db.cv_.notify_one();
        throw;
    }
}

Db::Lease::~Lease() {
    std::lock_guard<std::mutex> g(db.mu_);
    if (broken || !conn || !conn->is_open()) db.created_--;
    else db.idle_.push_back(std::move(conn));
    db.cv_.notify_one();
}

void Db::prepare_all(pqxx::connection& c) {
    // --- Read side ----------------------------------------------------------
    // v_inventory (which hides rows removed from the feed) does all the
    // joins; the service leans on views rather than re-implementing them.
    c.prepare("supplies_all",
        "SELECT item_id, name, general_name, manufacturer, category, quantity, "
        "       lot_number, expiration_date, is_expired, date_inventoried, "
        "       days_in_warehouse, box_label, pallet_number, "
        "       reserved_for_org, flagged, image_url "
        "FROM v_inventory ORDER BY item_id");

    // Same columns, every filter optional (NULL = don't filter).
    //   $1 category name or id   $2 box label   $3 name substring
    //   $4 expiring within N days   $5 flagged   $6 limit   $7 offset
    c.prepare("supplies_filtered",
        "SELECT item_id, name, general_name, manufacturer, category, quantity, "
        "       lot_number, expiration_date, is_expired, date_inventoried, "
        "       days_in_warehouse, box_label, pallet_number, "
        "       reserved_for_org, flagged, image_url "
        "FROM v_inventory "
        "WHERE ($1::text IS NULL OR lower(category::text) = lower($1) OR category_id::text = $1) "
        "  AND ($2::text IS NULL OR lower(box_label) = lower($2)) "
        "  AND ($3::text IS NULL OR name ILIKE '%' || $3 || '%') "
        "  AND ($4::int  IS NULL OR expiration_date < CURRENT_DATE + $4::int) "
        "  AND ($5::bool IS NULL OR flagged = $5) "
        "ORDER BY item_id LIMIT $6 OFFSET $7");

    c.prepare("health_counts",
        "SELECT (SELECT count(*) FROM inventory_item WHERE removed_at IS NULL), "
        "       (SELECT count(*) FROM supply), "
        "       (SELECT count(*) FROM shipment)");

    c.prepare("kpis", "SELECT row_to_json(k)::text FROM v_kpis k");

    // Timestamp is formatted to match the Render backend byte-for-byte:
    // ISO-8601 with microseconds and a literal "+00:00" offset.
    c.prepare("requests_all",
        "SELECT sh.request_id, r.org_name, r.org_email, si.item_name, "
        "       c.name AS category, si.quantity_requested, sh.status, "
        "       to_char(sh.requested_at AT TIME ZONE 'UTC', "
        "               'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"') AS ts "
        "FROM shipment_item si "
        "JOIN shipment sh USING (shipment_id) "
        "JOIN requester r USING (requester_id) "
        "LEFT JOIN category c ON c.category_id = si.category_id "
        "ORDER BY sh.requested_at, sh.shipment_id, si.line_no");

    // Availability rule reverse-engineered from the live backend (verified
    // against all 1384 rows): reserved -> Requested; quantity <= 5 or not a
    // clean integer in the sheet -> Limited ('Low Stock'); else Available.
    c.prepare("availability",
        "SELECT i.item_id, COALESCE(s.name, i.raw_name) AS item_name, "
        "       COALESCE(c.name, 'Unspecified') AS category, i.quantity, "
        "       req.org_name AS requesting_org, sh.request_id "
        "FROM inventory_item i "
        "LEFT JOIN supply s ON s.supply_id = i.supply_id "
        "LEFT JOIN category c ON c.category_id = s.category_id "
        "LEFT JOIN shipment sh ON sh.shipment_id = i.reserved_for "
        "LEFT JOIN requester req ON req.requester_id = sh.requester_id "
        "WHERE i.removed_at IS NULL "
        "ORDER BY i.item_id");

    // --- Expiration alerts ----------------------------------------------------
    c.prepare("expiring",
        "SELECT item_id, name, manufacturer, category, quantity, lot_number, "
        "       expiration_date, days_left, bucket, box_label, pallet_number, "
        "       reserved_for_org "
        "FROM v_expiring WHERE expiration_date < CURRENT_DATE + $1::int "
        "ORDER BY expiration_date, item_id");

    // --- Review queue -------------------------------------------------------
    c.prepare("review_queue",
        "SELECT q.item_id, q.raw_name, q.match_text, q.current_supply, q.quantity, "
        "       q.lot_number, q.expiration_date, q.box_label, q.image_url, q.notes, "
        "       COALESCE((SELECT json_agg(g) FROM suggest_supplies(q.match_text, 3) g), "
        "                '[]'::json) AS suggestions "
        "FROM v_review_queue q ORDER BY q.item_id LIMIT $1 OFFSET $2");

    c.prepare("review_count", "SELECT count(*) FROM v_review_queue");

    c.prepare("review_link",
        "UPDATE inventory_item SET supply_id = $2, flagged = false, reviewed_at = now() "
        "WHERE item_id = $1 AND removed_at IS NULL RETURNING item_id");

    c.prepare("supply_exists", "SELECT 1 FROM supply WHERE supply_id = $1");

    // --- Catalog ------------------------------------------------------------
    c.prepare("duplicates",
        "SELECT supply_a, name_a, mfr_a, supply_b, name_b, mfr_b, score, "
        "       (SELECT count(*) FROM inventory_item i WHERE i.supply_id = supply_a) AS items_a, "
        "       (SELECT count(*) FROM inventory_item i WHERE i.supply_id = supply_b) AS items_b "
        "FROM v_duplicate_supplies ORDER BY score DESC, supply_a LIMIT $1");

    c.prepare("merge_supply", "SELECT merge_supply($1, $2)");

    // --- Shipments / fulfillment --------------------------------------------
    c.prepare("shipments_list",
        "SELECT sh.shipment_id, sh.request_id, r.org_name, sh.status, "
        "       sh.requested_at::text, "
        "       (SELECT count(*) FROM shipment_item si WHERE si.shipment_id = sh.shipment_id) AS lines, "
        "       (SELECT count(*) FROM inventory_item i WHERE i.reserved_for = sh.shipment_id) AS reserved_lots "
        "FROM shipment sh JOIN requester r USING (requester_id) "
        "ORDER BY sh.requested_at DESC");

    c.prepare("shipment_resolve",
        "SELECT shipment_id FROM shipment WHERE request_id = $1 OR shipment_id::text = $1");

    c.prepare("shipment_lines",
        "SELECT si.shipment_item_id, si.line_no, si.item_name, si.quantity_requested, "
        "       si.supply_id, s.name AS matched_name "
        "FROM shipment_item si LEFT JOIN supply s USING (supply_id) "
        "WHERE si.shipment_id = $1 ORDER BY si.line_no");

    c.prepare("reserve", "SELECT * FROM reserve_shipment($1)");
    c.prepare("release", "SELECT release_shipment($1)");

    c.prepare("picklist",
        "SELECT pallet_number, box_label, item_id, name, manufacturer, lot_number, "
        "       expiration_date, quantity "
        "FROM v_picklist WHERE shipment_id = $1 "
        "ORDER BY pallet_number NULLS LAST, box_label NULLS LAST, item_id");

    // --- Boxes --------------------------------------------------------------
    c.prepare("boxes_all",
        "SELECT b.box_id, b.label, b.pallet_number, "
        "       count(v.item_id) AS lots, COALESCE(sum(v.quantity), 0) AS units "
        "FROM v_box_label b LEFT JOIN v_inventory v ON v.box_id = b.box_id "
        "GROUP BY b.box_id, b.label, b.pallet_number "
        "ORDER BY b.label");

    c.prepare("box_contents",
        "SELECT item_id, name, manufacturer, quantity, lot_number, expiration_date, "
        "       is_expired, flagged, reserved_for_org "
        "FROM v_inventory WHERE lower(box_label) = lower($1) ORDER BY item_id");

    // --- Sync bookkeeping -----------------------------------------------------
    c.prepare("sync_run_start", "INSERT INTO sync_run DEFAULT VALUES RETURNING run_id");
    c.prepare("sync_run_finish",
        "UPDATE sync_run SET finished_at = now(), items_added = $2, items_updated = $3, "
        "       items_removed = $4, rows_skipped = $5, requests_added = $6, "
        "       lines_added = $7, status_changes = $8, supplies_feed_ok = $9, "
        "       requests_feed_ok = $10, warnings = $11::jsonb, error = $12 "
        "WHERE run_id = $1");
    c.prepare("sync_status",
        "SELECT row_to_json(r)::text FROM (SELECT * FROM sync_run ORDER BY run_id DESC LIMIT 1) r");

    // --- Normalization rules (migration 001 tables) --------------------------
    c.prepare("rules_categories",
        "SELECT lower(name::text), category_id FROM category "
        "UNION ALL SELECT lower(alias::text), category_id FROM category_alias");
    c.prepare("rules_mfr",
        "SELECT lower(a.alias::text), m.canonical_name::text "
        "FROM manufacturer_alias a JOIN manufacturer m USING (manufacturer_id)");
    c.prepare("rules_box_types", "SELECT upper(code::text) FROM box_type");
    c.prepare("rules_box_fix", "SELECT upper(alias::text), code::text FROM box_type_alias");
    c.prepare("rules_status", "SELECT lower(alias::text), status FROM status_alias");

    // --- Sync writes ----------------------------------------------------------
    c.prepare("known_items",
        "SELECT source_key, item_id, source_hash, quantity, removed_at IS NOT NULL "
        "FROM inventory_item WHERE source_key IS NOT NULL");

    c.prepare("sync_counts",
        "SELECT (SELECT count(*) FROM inventory_item WHERE removed_at IS NULL), "
        "       (SELECT count(*) FROM supply), "
        "       (SELECT count(*) FROM shipment), "
        "       (SELECT coalesce(sum(quantity),0) FROM inventory_item WHERE removed_at IS NULL)");

    c.prepare("mfr_upsert",
        "INSERT INTO manufacturer (canonical_name) VALUES ($1) "
        "ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name "
        "RETURNING manufacturer_id");

    c.prepare("supply_upsert",
        "INSERT INTO supply (manufacturer_id, category_id, general_name, name) "
        "VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (manufacturer_id, name) DO UPDATE "
        "   SET general_name = COALESCE(supply.general_name, EXCLUDED.general_name) "
        "RETURNING supply_id");

    c.prepare("box_upsert",
        "INSERT INTO box (box_type, box_number, pallet_number) VALUES ($1,$2,$3) "
        "ON CONFLICT (box_type, box_number) DO UPDATE "
        "   SET pallet_number = COALESCE(box.pallet_number, EXCLUDED.pallet_number) "
        "RETURNING box_id");

    // New lots start at quantity 0; the opening balance is a 'received'
    // ledger row whose trigger sets the real quantity.
    c.prepare("item_insert",
        "INSERT INTO inventory_item (supply_id, box_id, quantity, lot_number, "
        " expiration_date, flagged, raw_name, image_url, notes, source_hash, source_key, "
        " warehouse_date) "
        "VALUES ($1,$2,0,$3,$4,$5,$6,$7,$8,$9,$10, CURRENT_DATE - COALESCE($11::int, 0)) "
        "RETURNING item_id");

    // An edited feed row. A human review decision (reviewed_at) wins over the
    // feed's flag/catalog fields; everything else follows the feed.
    c.prepare("item_update",
        "UPDATE inventory_item SET "
        "  supply_id  = CASE WHEN reviewed_at IS NULL THEN $2 ELSE supply_id END, "
        "  flagged    = CASE WHEN reviewed_at IS NULL THEN $6 ELSE flagged END, "
        "  raw_name   = CASE WHEN reviewed_at IS NULL THEN $7 ELSE raw_name END, "
        "  box_id = $3, lot_number = $4, expiration_date = $5, image_url = $8, "
        "  notes = $9, source_hash = $10, removed_at = NULL "
        "WHERE item_id = $1");

    c.prepare("item_remove",
        "UPDATE inventory_item SET removed_at = now() WHERE item_id = $1");

    c.prepare("txn_insert",
        "INSERT INTO inventory_transaction (item_id, txn_type, delta, note, source) "
        "VALUES ($1,$2,$3,$4,$5)");

    c.prepare("requester_upsert",
        "INSERT INTO requester (org_name, org_email) VALUES ($1,$2) "
        "ON CONFLICT (org_name) DO UPDATE SET org_email = COALESCE(requester.org_email, EXCLUDED.org_email) "
        "RETURNING requester_id");

    c.prepare("shipment_by_req",
        "SELECT shipment_id, status FROM shipment WHERE request_id = $1");

    c.prepare("shipment_insert",
        "INSERT INTO shipment (requester_id, request_id, status, requested_at) "
        "VALUES ($1,$2,$3,COALESCE($4::timestamptz, now())) RETURNING shipment_id");

    c.prepare("shipment_status_update",
        "UPDATE shipment SET status = $1 WHERE shipment_id = $2");

    // One row per (shipment, position in the request). Returns a row only if
    // something was inserted or changed; `inserted` tells which.
    c.prepare("shipment_item_upsert",
        "INSERT INTO shipment_item (shipment_id, line_no, item_name, category_id, quantity_requested) "
        "VALUES ($1,$2,$3,$4,$5) "
        "ON CONFLICT (shipment_id, line_no) DO UPDATE SET "
        "   item_name = EXCLUDED.item_name, category_id = EXCLUDED.category_id, "
        "   quantity_requested = EXCLUDED.quantity_requested "
        "WHERE (shipment_item.item_name, shipment_item.category_id, shipment_item.quantity_requested) "
        "      IS DISTINCT FROM (EXCLUDED.item_name, EXCLUDED.category_id, EXCLUDED.quantity_requested) "
        "RETURNING (xmax = 0) AS inserted");
}
