#include "sync.h"

#include <atomic>
#include <cstdio>
#include <functional>
#include <optional>
#include <set>
#include <string>

#include <drogon/drogon.h>
#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>

#include "db.h"
#include "normalize.h"

using nlohmann::json;

namespace {

constexpr auto kBackendHost  = "https://b4p-external-view-w26-backend.onrender.com";
constexpr auto kSuppliesPath = "/api/supplies";
constexpr auto kRequestsPath = "/requests";
constexpr double kFetchTimeoutSec = 90.0;  // matches monitor.py's `curl -m 90`

// ---------------------------------------------------------------------------
// Canonical row hashing.
//
// monitor.py identifies a feed row by md5(json.dumps(row, sort_keys=True)).
// The database already holds those hashes, so this port MUST produce the
// identical bytes or every historical row would look "new" and be re-inserted.
// Python's json.dumps defaults differ from nlohmann::json::dump() in two ways
// we have to reproduce:
//   * separators are ", " and ": " (nlohmann emits "," and ":")
//   * ensure_ascii=True — every char outside 0x20..0x7e becomes \uXXXX
//     (surrogate pairs above the BMP); nlohmann keeps raw UTF-8.
// Key order is fine for free: nlohmann objects iterate sorted (std::map) and
// the feed keys are ASCII, where byte order == codepoint order.
// ---------------------------------------------------------------------------

void py_escape_string(const std::string& s, std::string& out) {
    out += '"';
    size_t i = 0, n = s.size();
    auto emit_u16 = [&](unsigned cp) {
        char buf[8];
        snprintf(buf, sizeof buf, "\\u%04x", cp);
        out += buf;
    };
    while (i < n) {
        unsigned char c = s[i];
        if (c == '"')       { out += "\\\""; i++; }
        else if (c == '\\') { out += "\\\\"; i++; }
        else if (c == '\n') { out += "\\n"; i++; }
        else if (c == '\r') { out += "\\r"; i++; }
        else if (c == '\t') { out += "\\t"; i++; }
        else if (c == '\b') { out += "\\b"; i++; }
        else if (c == '\f') { out += "\\f"; i++; }
        else if (c >= 0x20 && c <= 0x7e) { out += static_cast<char>(c); i++; }
        else if (c < 0x80) { emit_u16(c); i++; }  // control chars, DEL
        else {
            // decode one UTF-8 sequence to a codepoint
            unsigned cp = 0; int len = 0;
            if      ((c & 0xe0) == 0xc0) { cp = c & 0x1f; len = 2; }
            else if ((c & 0xf0) == 0xe0) { cp = c & 0x0f; len = 3; }
            else if ((c & 0xf8) == 0xf0) { cp = c & 0x07; len = 4; }
            else { emit_u16(0xfffd); i++; continue; }
            if (i + len > n) { emit_u16(0xfffd); i++; continue; }
            for (int k = 1; k < len; k++) cp = (cp << 6) | (s[i + k] & 0x3f);
            i += len;
            if (cp <= 0xffff) emit_u16(cp);
            else {  // surrogate pair, exactly like Python
                cp -= 0x10000;
                emit_u16(0xd800 + (cp >> 10));
                emit_u16(0xdc00 + (cp & 0x3ff));
            }
        }
    }
    out += '"';
}

void py_dump(const json& v, std::string& out) {
    switch (v.type()) {
        case json::value_t::null:    out += "null"; break;
        case json::value_t::boolean: out += v.get<bool>() ? "true" : "false"; break;
        case json::value_t::string:  py_escape_string(v.get<std::string>(), out); break;
        case json::value_t::object: {
            out += '{';
            bool first = true;
            for (auto it = v.begin(); it != v.end(); ++it) {  // sorted (std::map)
                if (!first) out += ", ";
                first = false;
                py_escape_string(it.key(), out);
                out += ": ";
                py_dump(it.value(), out);
            }
            out += '}';
            break;
        }
        case json::value_t::array: {
            out += '[';
            bool first = true;
            for (const auto& e : v) {
                if (!first) out += ", ";
                first = false;
                py_dump(e, out);
            }
            out += ']';
            break;
        }
        default:  // integer / unsigned / float: nlohmann's shortest-roundtrip
            out += v.dump();  // matches repr() for ints; feed values are strings
    }
}

std::string row_hash(const json& row) {
    std::string canon;
    py_dump(row, canon);
    std::string h = drogon::utils::getMd5(canon);
    for (auto& c : h) c = std::tolower(static_cast<unsigned char>(c));
    return h;
}

// str(row.get(key) or "") — the feed is sheet-backed so values are strings,
// but be tolerant of numbers/bools/null exactly like Python's str().
std::string text(const json& row, const char* key) {
    auto it = row.find(key);
    if (it == row.end() || it->is_null()) return "";
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

std::optional<std::string> opt(const std::string& s) {
    std::string t = normalize::strip(s);
    if (t.empty()) return std::nullopt;
    return t;
}

// ---------------------------------------------------------------------------
// Sync passes — line-for-line ports of monitor.py's sync_supplies() and
// sync_requests(). Both run inside the SAME pqxx::work, so a cycle either
// lands completely or not at all.
// ---------------------------------------------------------------------------

int sync_supplies(pqxx::work& tx, const json& rows) {
    std::set<std::string> known;
    for (const auto& r : tx.exec(pqxx::prepped{"known_hashes"}))
        known.insert(r[0].as<std::string>());

    int added = 0;
    for (const auto& row : rows) {
        std::string h = row_hash(row);
        if (known.count(h)) continue;
        std::string name = normalize::strip(text(row, "Name"));
        if (name.empty()) continue;

        std::optional<long> mid;
        if (auto mfr = normalize::norm_mfr(text(row, "Manufacturer Name")))
            mid = tx.exec(pqxx::prepped{"mfr_upsert"}, pqxx::params{*mfr})
                    .one_row()[0].as<long>();

        long sid = tx.exec(pqxx::prepped{"supply_upsert"},
                pqxx::params{mid, normalize::norm_category(text(row, "Category")),
                             opt(text(row, "General")), name})
                .one_row()[0].as<long>();

        std::optional<long> bid;
        if (auto box = normalize::parse_box(text(row, "Box Number")))
            bid = tx.exec(pqxx::prepped{"box_upsert"},
                    pqxx::params{box->first, box->second,
                                 normalize::parse_pallet(text(row, "Pallet Number"))})
                    .one_row()[0].as<long>();

        bool flagged = name[0] == '{' ||
            !(normalize::strip(text(row, "Review")).empty() ||
              normalize::strip(text(row, "Review")) == "No review needed");
        std::string lot = normalize::strip(text(row, "Lot Number"));
        std::optional<std::string> lot_opt;
        if (!lot.empty() && normalize::lower(lot) != "unspecified") lot_opt = lot;

        long qty = normalize::parse_qty(text(row, "Quantity"));
        tx.exec(pqxx::prepped{"item_insert"},
                pqxx::params{sid, bid, qty, lot_opt,
                             normalize::parse_date(text(row, "Date of Expiration")),
                             flagged,
                             flagged ? std::optional<std::string>(name) : std::nullopt,
                             opt(text(row, "Image URL")), opt(text(row, "Notes")), h});
        added++;
        spdlog::info("NEW item in: '{}' qty={} box={}{}", name.substr(0, 60), qty,
                     text(row, "Box Number").empty() ? "-" : text(row, "Box Number"),
                     flagged ? " FLAGGED" : "");
    }
    return added;
}

int sync_requests(pqxx::work& tx, const json& rows) {
    int added = 0;
    for (const auto& row : rows) {
        std::string org = normalize::strip(text(row, "Org Name"));
        std::string rid = normalize::strip(text(row, "Request ID"));
        if (org.empty() || rid.empty()) continue;

        long qid = tx.exec(pqxx::prepped{"requester_upsert"},
                pqxx::params{org, opt(text(row, "Org Email"))})
                .one_row()[0].as<long>();

        std::string status = normalize::map_status(text(row, "Status"));
        auto hit = tx.exec(pqxx::prepped{"shipment_by_req"}, pqxx::params{rid});
        if (hit.empty()) {
            long sid = tx.exec(pqxx::prepped{"shipment_insert"},
                    pqxx::params{qid, rid, status, opt(text(row, "Timestamp"))})
                    .one_row()[0].as<long>();
            long q = normalize::parse_qty(text(row, "Quantity Requested"));
            std::string item = normalize::strip(text(row, "Item Name"));
            tx.exec(pqxx::prepped{"shipment_item_insert"},
                    pqxx::params{sid, item.empty() ? "?" : item,
                                 normalize::norm_category(text(row, "Category")),
                                 q ? std::optional<long>(q) : std::nullopt});
            spdlog::info("NEW request {} from '{}': {}", rid, org,
                         text(row, "Item Name").substr(0, 50));
            added++;
        } else if (hit[0][1].as<std::string>() != status) {
            tx.exec(pqxx::prepped{"shipment_status_update"},
                    pqxx::params{status, hit[0][0].as<long>()});
            spdlog::info("CHANGE request {} ({}): {} -> {}", rid, org,
                         hit[0][1].as<std::string>(), status);
        }
    }
    return added;
}

// ---------------------------------------------------------------------------
// Consistency checks — the CHECKS list from monitor.py, verbatim. Each runs
// in its OWN transaction: a failed statement poisons a pqxx transaction, and
// one broken check must not take the others down (monitor.py had the same
// per-check try/except).
// ---------------------------------------------------------------------------

const std::pair<const char*, const char*> kChecks[] = {
    {"negative stock (ledger pushed quantity below zero)",
     "SELECT count(*) FROM inventory_item WHERE quantity < 0 HAVING count(*) > 0"},
    {"items reserved for a cancelled/delivered shipment",
     "SELECT i.item_id FROM inventory_item i JOIN shipment s ON s.shipment_id = i.reserved_for "
     "WHERE s.status IN ('cancelled','delivered') LIMIT 20"},
    {"expired stock not yet flagged or reserved",
     "SELECT count(*) FROM inventory_item WHERE expiration_date < CURRENT_DATE "
     "AND NOT flagged AND reserved_for IS NULL HAVING count(*) > 0"},
    {"supplies with duplicate names under different manufacturers",
     "SELECT lower(name) FROM supply GROUP BY lower(name) "
     "HAVING count(DISTINCT manufacturer_id) > 1 LIMIT 20"},
    {"inventory items with zero quantity still in a box",
     "SELECT count(*) FROM inventory_item WHERE quantity = 0 AND box_id IS NOT NULL "
     "HAVING count(*) > 0"},
    {"flagged items awaiting review",
     "SELECT count(*) FROM inventory_item WHERE flagged HAVING count(*) > 0"},
};

void run_checks(Db& db) {
    for (const auto& [label, sql] : kChecks) {
        try {
            auto rows = db.txn([sql = sql](pqxx::work& tx) { return tx.exec(sql); });
            if (!rows.empty()) {
                std::string detail =
                    (rows.size() == 1 && rows[0].size() == 1)
                        ? rows[0][0].as<std::string>()
                        : std::to_string(rows.size()) + " cases";
                spdlog::warn("{}: {}", label, detail);
            }
        } catch (const std::exception& e) {
            spdlog::error("check '{}' failed: {}", label, e.what());
        }
    }
}

struct Counts { long items, supplies, shipments, units; };

Counts counts(Db& db) {
    auto r = db.txn([](pqxx::work& tx) {
        return tx.exec(pqxx::prepped{"sync_counts"}).one_row();
    });
    return {r[0].as<long>(), r[1].as<long>(), r[2].as<long>(), r[3].as<long>()};
}

// ---------------------------------------------------------------------------
// The async cycle. drogon::HttpClient callbacks fire on the same event loop
// the client was created on (the main loop here), so the whole chain —
// fetch supplies -> fetch requests -> one write transaction -> checks —
// runs sequentially with no locking beyond Db's own mutex.
// ---------------------------------------------------------------------------

std::atomic<bool> g_running{false};

void fetch(const std::string& path,
           std::function<void(std::optional<json>)> done) {
    auto client = drogon::HttpClient::newHttpClient(
        kBackendHost, drogon::app().getLoop());
    auto req = drogon::HttpRequest::newHttpRequest();
    req->setPath(path);
    client->sendRequest(req,
        // capture the client so it outlives the request
        [client, path, done = std::move(done)](drogon::ReqResult res,
                                               const drogon::HttpResponsePtr& resp) {
            if (res != drogon::ReqResult::Ok || !resp ||
                resp->statusCode() != drogon::k200OK) {
                spdlog::error("fetch {} failed: {}", path,
                              res == drogon::ReqResult::Ok
                                  ? "HTTP " + std::to_string(resp ? resp->statusCode() : 0)
                                  : to_string(res));
                done(std::nullopt);
                return;
            }
            try {
                done(json::parse(resp->body()));
            } catch (const std::exception& e) {
                spdlog::error("fetch {}: bad JSON: {}", path, e.what());
                done(std::nullopt);
            }
        },
        kFetchTimeoutSec);
}

void finish_cycle(std::shared_ptr<Db> db,
                  std::optional<json> supplies, std::optional<json> requests) {
    try {
        Counts before = counts(*db);
        int n_items = 0, n_reqs = 0;
        db->txn([&](pqxx::work& tx) {
            if (supplies) n_items = sync_supplies(tx, *supplies);
            if (requests) n_reqs = sync_requests(tx, *requests);
        });
        Counts after = counts(*db);
        if (n_items || n_reqs)
            spdlog::info("SYNC +{} items, +{} requests (items {}->{}, units {}->{})",
                         n_items, n_reqs, before.items, after.items,
                         before.units, after.units);
        else
            spdlog::info("OK in sync: {} items / {} units / {} shipments",
                         after.items, after.units, after.shipments);
        run_checks(*db);
    } catch (const std::exception& e) {
        spdlog::error("sync cycle failed: {}", e.what());
    }
    g_running = false;
}

}  // namespace

namespace sync {

void run_cycle(std::shared_ptr<Db> db) {
    bool expected = false;
    if (!g_running.compare_exchange_strong(expected, true)) {
        spdlog::warn("sync cycle skipped: previous cycle still running");
        return;
    }
    fetch(kSuppliesPath, [db](std::optional<json> supplies) {
        fetch(kRequestsPath, [db, supplies = std::move(supplies)](
                                 std::optional<json> requests) {
            finish_cycle(db, supplies, std::move(requests));
        });
    });
}

void start(std::shared_ptr<Db> db, double interval_sec) {
    drogon::app().getLoop()->runAfter(1.0, [db] { run_cycle(db); });
    drogon::app().getLoop()->runEvery(interval_sec, [db] { run_cycle(db); });
    spdlog::info("sync scheduled every {}s against {}", interval_sec, kBackendHost);
}

}  // namespace sync
