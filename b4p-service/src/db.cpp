#include "db.h"
#include <spdlog/spdlog.h>

Db::Db(const std::string& conninfo) : conninfo_(conninfo) {
    ensure_open();  // fail fast at startup if the DB is unreachable
}

void Db::ensure_open() {
    // pqxx::connection::is_open() goes false if Postgres restarted underneath
    // us; reconnecting lazily here means the service survives a `brew services
    // restart postgresql@16` without needing its own restart.
    if (conn_ && conn_->is_open()) return;
    spdlog::info("connecting to postgres: {}", conninfo_);
    conn_ = std::make_unique<pqxx::connection>(conninfo_);
    prepare_all();
}

void Db::prepare_all() {
    // --- Read side (milestone 2) -------------------------------------------
    // v_inventory already exists in the schema and does all the joins; the
    // service should lean on views rather than re-implementing joins in C++.
    conn_->prepare("supplies_all",
        "SELECT item_id, name, general_name, manufacturer, category, quantity, "
        "       lot_number, expiration_date, is_expired, date_inventoried, "
        "       days_in_warehouse, box_label, pallet_number, "
        "       reserved_for_org, flagged, image_url "
        "FROM v_inventory ORDER BY item_id");

    conn_->prepare("health_counts",
        "SELECT (SELECT count(*) FROM inventory_item), "
        "       (SELECT count(*) FROM supply), "
        "       (SELECT count(*) FROM shipment)");

    // --- Milestone 2: dashboard-compatible read endpoints -------------------
    // Timestamp is formatted to match the Render backend byte-for-byte:
    // ISO-8601 with microseconds and a literal "+00:00" offset.
    conn_->prepare("requests_all",
        "SELECT sh.request_id, r.org_name, r.org_email, si.item_name, "
        "       c.name AS category, si.quantity_requested, sh.status, "
        "       to_char(sh.requested_at AT TIME ZONE 'UTC', "
        "               'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"') AS ts "
        "FROM shipment_item si "
        "JOIN shipment sh USING (shipment_id) "
        "JOIN requester r USING (requester_id) "
        "LEFT JOIN category c ON c.category_id = si.category_id "
        "ORDER BY sh.requested_at, si.shipment_item_id");

    // Availability rule reverse-engineered from the live backend (verified
    // against all 1384 rows): reserved -> Requested; quantity <= 5 or not a
    // clean integer in the sheet -> Limited ('Low Stock'); else Available.
    // We store parsed quantities, so "clean integer" is approximated by the
    // stored value — see README for the one known divergence.
    conn_->prepare("availability",
        "SELECT i.item_id, COALESCE(s.name, i.raw_name) AS item_name, "
        "       COALESCE(c.name, 'Unspecified') AS category, i.quantity, "
        "       req.org_name AS requesting_org, sh.request_id "
        "FROM inventory_item i "
        "LEFT JOIN supply s ON s.supply_id = i.supply_id "
        "LEFT JOIN category c ON c.category_id = s.category_id "
        "LEFT JOIN shipment sh ON sh.shipment_id = i.reserved_for "
        "LEFT JOIN requester req ON req.requester_id = sh.requester_id "
        "ORDER BY i.item_id");

    // --- Milestone 4: sync statements, ported verbatim from monitor.py ------
    conn_->prepare("known_hashes",
        "SELECT source_hash FROM inventory_item WHERE source_hash IS NOT NULL");

    conn_->prepare("sync_counts",
        "SELECT (SELECT count(*) FROM inventory_item), "
        "       (SELECT count(*) FROM supply), "
        "       (SELECT count(*) FROM shipment), "
        "       (SELECT coalesce(sum(quantity),0) FROM inventory_item)");

    conn_->prepare("mfr_upsert",
        "INSERT INTO manufacturer (canonical_name) VALUES ($1) "
        "ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name "
        "RETURNING manufacturer_id");

    conn_->prepare("supply_upsert",
        "INSERT INTO supply (manufacturer_id, category_id, general_name, name) "
        "VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (manufacturer_id, name) DO UPDATE SET name = EXCLUDED.name "
        "RETURNING supply_id");

    conn_->prepare("box_upsert",
        "INSERT INTO box (box_type, box_number, pallet_number) VALUES ($1,$2,$3) "
        "ON CONFLICT (box_type, box_number) DO UPDATE SET box_number = EXCLUDED.box_number "
        "RETURNING box_id");

    conn_->prepare("item_insert",
        "INSERT INTO inventory_item (supply_id, box_id, quantity, lot_number, "
        " expiration_date, flagged, raw_name, image_url, notes, source_hash) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
        "ON CONFLICT (source_hash) DO NOTHING");

    conn_->prepare("requester_upsert",
        "INSERT INTO requester (org_name, org_email) VALUES ($1,$2) "
        "ON CONFLICT (org_name) DO UPDATE SET org_email = COALESCE(requester.org_email, EXCLUDED.org_email) "
        "RETURNING requester_id");

    conn_->prepare("shipment_by_req",
        "SELECT shipment_id, status FROM shipment WHERE request_id = $1");

    conn_->prepare("shipment_insert",
        "INSERT INTO shipment (requester_id, request_id, status, requested_at) "
        "VALUES ($1,$2,$3,COALESCE($4::timestamptz, now())) RETURNING shipment_id");

    conn_->prepare("shipment_status_update",
        "UPDATE shipment SET status = $1 WHERE shipment_id = $2");

    conn_->prepare("shipment_item_insert",
        "INSERT INTO shipment_item (shipment_id, item_name, category_id, quantity_requested) "
        "VALUES ($1,$2,$3,$4)");
}
