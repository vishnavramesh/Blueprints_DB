// b4p-service — C++ inventory service for the Blueprints for Pangaea database.
//
// MILESTONE 1 (this file, working): HTTP server + Postgres.
//   GET /health        -> row counts, proves DB connectivity
//   GET /api/supplies  -> full inventory as JSON from v_inventory
//
// Build:   cmake -B build && cmake --build build
// Run:     ./build/b4p-service          (listens on :8080, connects to b4p)
// Try:     curl localhost:8080/health
//
// ROADMAP (see also comments in db.cpp / CMakeLists.txt):
//   MILESTONE 2: /requests + /inventory/availability, byte-compatible with the
//     Render backend so the dashboard frontend can be pointed at this service.
//     Diff outputs with:  curl -s <render>/requests | jq -S . > a.json
//                         curl -s localhost:8080/requests | jq -S . > b.json
//   MILESTONE 3: normalize.{h,cpp} — port load.py's pure functions
//     (norm_category, norm_mfr, parse_box, parse_qty, parse_date) + Catch2
//     tests using the nasty real-world inputs: "1 (full box)", "TC/RC 11",
//     "MISC 2", bare "8" -> LEGACY-8, the unicode-dash "Misc Non‐Medical".
//   MILESTONE 4: sync.{h,cpp} — port monitor.py: fetch both feeds with
//     drogon::HttpClient, md5 each row (source_hash), insert unseen rows in
//     ONE transaction, then run the consistency checks (checks.cpp).
//     Schedule with the runEvery() timer below. Then retire monitor.py and
//     point the launchd plist at this binary instead.
//   MILESTONE 5: hardening — connection pool in Db, config file instead of
//     the constants below, graceful shutdown, structured error responses.

#include <map>

#include <drogon/drogon.h>
#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>
#include "db.h"
#include "sync.h"

using nlohmann::json;

// MILESTONE 5: move to config.json (drogon::app().loadConfigFile also works).
static constexpr auto kConnInfo    = "dbname=b4p";
static constexpr int  kPort        = 8080;
static constexpr double kSyncEvery = 300.0;  // seconds, matches monitor.py

// shipment.status enum value -> the display text the Render backend emits.
static std::string status_display(const std::string& s) {
    static const std::map<std::string, std::string> kMap = {
        {"requested", "Requested"},   {"under_review", "Under Review"},
        {"approved", "Approved"},     {"packing", "Packing"},
        {"shipped", "Shipped"},       {"delivered", "Delivered"},
        {"cancelled", "Cancelled"},
    };
    auto it = kMap.find(s);
    return it != kMap.end() ? it->second : s;
}

// Convert one pqxx row field to JSON, preserving NULLs as null (the Python
// backend emits null for missing values; keep the contract identical).
// Template because libpqxx 7.10 iterates rows as pqxx::field_ref, while older
// versions hand out pqxx::field — this accepts either.
template <typename Field>
static json field_json(const Field& f) {
    if (f.is_null()) return nullptr;
    return f.template as<std::string>();  // string-typed for now; refine per-column in M2
}

int main() {
    spdlog::info("b4p-service starting on :{}", kPort);

    // Db is created before the server starts: if Postgres is down we exit
    // immediately with a clear error instead of serving 500s.
    auto db = std::make_shared<Db>(kConnInfo);

    drogon::app().registerHandler("/health",
        [db](const drogon::HttpRequestPtr&,
             std::function<void(const drogon::HttpResponsePtr&)>&& cb) {
            json j;
            try {
                auto row = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"health_counts"}).one_row();
                });
                j = {{"status", "ok"},
                     {"inventory_items", row[0].as<long>()},
                     {"supplies", row[1].as<long>()},
                     {"shipments", row[2].as<long>()}};
            } catch (const std::exception& e) {
                spdlog::error("/health: {}", e.what());
                j = {{"status", "degraded"}, {"error", e.what()}};
            }
            auto resp = drogon::HttpResponse::newHttpResponse();
            resp->setContentTypeCode(drogon::CT_APPLICATION_JSON);
            resp->setBody(j.dump());
            cb(resp);
        });

    drogon::app().registerHandler("/api/supplies",
        [db](const drogon::HttpRequestPtr&,
             std::function<void(const drogon::HttpResponsePtr&)>&& cb) {
            auto resp = drogon::HttpResponse::newHttpResponse();
            resp->setContentTypeCode(drogon::CT_APPLICATION_JSON);
            try {
                auto rows = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"supplies_all"});
                });
                json out = json::array();
                for (const auto& r : rows) {
                    json item;
                    for (const auto& f : r) item[f.name()] = field_json(f);
                    out.push_back(std::move(item));
                }
                resp->setBody(out.dump());
            } catch (const std::exception& e) {
                spdlog::error("/api/supplies: {}", e.what());
                resp->setStatusCode(drogon::k500InternalServerError);
                resp->setBody(json{{"error", e.what()}}.dump());
            }
            cb(resp);
        });

    // MILESTONE 2: /requests — same shape as the Render backend, one JSON
    // object per shipment line item. "Review Flag" is not stored in the
    // schema, so it is emitted as the constant "FALSE" (see README).
    drogon::app().registerHandler("/requests",
        [db](const drogon::HttpRequestPtr&,
             std::function<void(const drogon::HttpResponsePtr&)>&& cb) {
            auto resp = drogon::HttpResponse::newHttpResponse();
            resp->setContentTypeCode(drogon::CT_APPLICATION_JSON);
            try {
                auto rows = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"requests_all"});
                });
                json out = json::array();
                for (const auto& r : rows) {
                    out.push_back({
                        {"Request ID", r[0].as<std::string>()},
                        {"Org Name",   r[1].as<std::string>()},
                        {"Org Email",  r[2].is_null() ? "" : r[2].as<std::string>()},
                        {"Item Name",  r[3].as<std::string>()},
                        {"Category",   r[4].is_null() ? "" : r[4].as<std::string>()},
                        {"Quantity Requested",
                                       r[5].is_null() ? "" : r[5].as<std::string>()},
                        {"Status",     status_display(r[6].as<std::string>())},
                        {"Timestamp",  r[7].as<std::string>()},
                        {"Review Flag", "FALSE"},
                    });
                }
                resp->setBody(out.dump());
            } catch (const std::exception& e) {
                spdlog::error("/requests: {}", e.what());
                resp->setStatusCode(drogon::k500InternalServerError);
                resp->setBody(json{{"error", e.what()}}.dump());
            }
            cb(resp);
        });

    // MILESTONE 2: /inventory/availability — the Render backend's rule,
    // verified against all live rows: reserved -> "Requested" (tagged with the
    // org), quantity <= 5 -> "Limited"/"Low Stock", else "Available".
    // We key rows by item_id where the Render backend uses the raw sheet_row.
    drogon::app().registerHandler("/inventory/availability",
        [db](const drogon::HttpRequestPtr&,
             std::function<void(const drogon::HttpResponsePtr&)>&& cb) {
            auto resp = drogon::HttpResponse::newHttpResponse();
            resp->setContentTypeCode(drogon::CT_APPLICATION_JSON);
            try {
                auto rows = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"availability"});
                });
                json out = json::array();
                for (const auto& r : rows) {
                    bool reserved = !r[4].is_null();
                    long qty = r[3].as<long>();
                    std::string status = reserved  ? "Requested"
                                       : qty <= 5  ? "Limited"
                                                   : "Available";
                    std::string tag = reserved
                        ? "Requested by " + r[4].as<std::string>()
                        : qty <= 5 ? "Low Stock" : "Available";
                    out.push_back({
                        {"item_id",             r[0].as<long>()},
                        {"item_name",           field_json(r[1])},
                        {"category",            r[2].as<std::string>()},
                        {"availability_status", status},
                        {"tags",                json::array({tag})},
                        {"requesting_org",      field_json(r[4])},
                        {"request_id",          field_json(r[5])},
                    });
                }
                resp->setBody(out.dump());
            } catch (const std::exception& e) {
                spdlog::error("/inventory/availability: {}", e.what());
                resp->setStatusCode(drogon::k500InternalServerError);
                resp->setBody(json{{"error", e.what()}}.dump());
            }
            cb(resp);
        });

    // MILESTONE 4: the sync loop hangs off Drogon's own event loop — no extra
    // thread, so Db's mutex is the only synchronization the whole service needs.
    sync::start(db, kSyncEvery);

    drogon::app().addListener("0.0.0.0", kPort).run();
    return 0;
}
