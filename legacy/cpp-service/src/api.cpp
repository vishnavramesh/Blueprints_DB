#include "api.h"

#include <climits>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>

#include <drogon/drogon.h>
#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>

#include "db.h"
#include "sync.h"

using nlohmann::json;
using drogon::HttpRequestPtr;
using drogon::HttpResponsePtr;
using Callback = std::function<void(const HttpResponsePtr&)>;

namespace {

// ---------------------------------------------------------------------------
// Response helpers
// ---------------------------------------------------------------------------

struct HttpError : std::runtime_error {
    HttpError(drogon::HttpStatusCode s, std::string c, const std::string& m)
        : std::runtime_error(m), status(s), code(std::move(c)) {}
    drogon::HttpStatusCode status;
    std::string code;
};

HttpError bad_request(const std::string& m) { return {drogon::k400BadRequest, "bad_request", m}; }
HttpError not_found(const std::string& m) { return {drogon::k404NotFound, "not_found", m}; }

HttpResponsePtr json_resp(const json& j, drogon::HttpStatusCode code = drogon::k200OK) {
    auto resp = drogon::HttpResponse::newHttpResponse();
    resp->setStatusCode(code);
    resp->setContentTypeCode(drogon::CT_APPLICATION_JSON);
    resp->setBody(j.dump());
    return resp;
}

HttpResponsePtr error_resp(drogon::HttpStatusCode code, const std::string& err,
                           const std::string& msg) {
    return json_resp({{"error", {{"code", err}, {"message", msg}}}}, code);
}

// Runs a handler body, mapping exceptions to structured errors. RAISE
// EXCEPTION from our plpgsql functions (merge_supply, reserve_shipment) and
// constraint violations are the caller's fault -> 409 with the DB's message;
// anything else is logged and reported as a generic 500.
template <typename Fn>
void handle(const char* route, const Callback& cb, Fn&& fn) {
    try {
        cb(fn());
    } catch (const HttpError& e) {
        cb(error_resp(e.status, e.code, e.what()));
    } catch (const pqxx::sql_error& e) {
        std::string state(e.sqlstate());
        if (state == "P0001" || state.rfind("23", 0) == 0) {  // raise_exception, integrity
            cb(error_resp(drogon::k409Conflict, "conflict", e.what()));
        } else {
            spdlog::error("{}: {}", route, e.what());
            cb(error_resp(drogon::k500InternalServerError, "internal", "database error"));
        }
    } catch (const std::exception& e) {
        spdlog::error("{}: {}", route, e.what());
        cb(error_resp(drogon::k500InternalServerError, "internal", e.what()));
    }
}

// ---------------------------------------------------------------------------
// Row -> JSON
// ---------------------------------------------------------------------------

// Dashboard-compatible encoding: every non-NULL value as a string, NULL as
// null (the Python backend's contract). Template because libpqxx iterates
// rows as pqxx::field_ref in newer versions and pqxx::field in older ones.
template <typename Field>
json field_json(const Field& f) {
    if (f.is_null()) return nullptr;
    return f.template as<std::string>();
}

// Typed encoding for the newer endpoints: numbers, booleans and json columns
// come out as real JSON types (by the column's type OID).
template <typename Field>
json typed_json(const Field& f) {
    if (f.is_null()) return nullptr;
    switch (f.type()) {
        case 16:                         return f.template as<bool>();          // bool
        case 20: case 21: case 23:       return f.template as<long long>();     // int8/2/4
        case 700: case 701: case 1700:   return f.template as<double>();        // float/numeric
        case 114: case 3802:             return json::parse(f.template as<std::string>());
        default:                         return f.template as<std::string>();
    }
}

json rows_typed(const pqxx::result& rows) {
    json out = json::array();
    for (const auto& r : rows) {
        json item = json::object();
        for (const auto& f : r) item[f.name()] = typed_json(f);
        out.push_back(std::move(item));
    }
    return out;
}

// ---------------------------------------------------------------------------
// Request parsing
// ---------------------------------------------------------------------------

std::optional<std::string> param(const HttpRequestPtr& req, const std::string& k) {
    const auto& v = req->getParameter(k);
    if (v.empty()) return std::nullopt;
    return v;
}

std::optional<long> int_param(const HttpRequestPtr& req, const std::string& k,
                              long lo, long hi) {
    auto v = param(req, k);
    if (!v) return std::nullopt;
    size_t used = 0;
    long n = 0;
    try { n = std::stol(*v, &used); } catch (...) { used = 0; }
    if (used != v->size() || n < lo || n > hi)
        throw bad_request(k + " must be an integer in [" + std::to_string(lo) + ", " +
                          std::to_string(hi) + "]");
    return n;
}

std::optional<bool> bool_param(const HttpRequestPtr& req, const std::string& k) {
    auto v = param(req, k);
    if (!v) return std::nullopt;
    if (*v == "true" || *v == "1") return true;
    if (*v == "false" || *v == "0") return false;
    throw bad_request(k + " must be true or false");
}

json body_json(const HttpRequestPtr& req) {
    try {
        auto j = json::parse(req->body());
        if (!j.is_object()) throw std::runtime_error("not an object");
        return j;
    } catch (...) {
        throw bad_request("body must be a JSON object");
    }
}

long body_id(const json& body, const char* key) {
    auto it = body.find(key);
    if (it == body.end() || !it->is_number_integer() || it->get<long>() <= 0)
        throw bad_request(std::string(key) + " must be a positive integer");
    return it->get<long>();
}

long path_id(const std::string& s, const char* what) {
    size_t used = 0;
    long n = 0;
    try { n = std::stol(s, &used); } catch (...) { used = 0; }
    if (used != s.size() || n <= 0) throw bad_request(std::string(what) + " must be a positive integer");
    return n;
}

long resolve_shipment(pqxx::work& tx, const std::string& ref) {
    auto r = tx.exec(pqxx::prepped{"shipment_resolve"}, pqxx::params{ref});
    if (r.empty()) throw not_found("no shipment " + ref);
    return r[0][0].as<long>();
}

// shipment.status enum value -> the display text the Render backend emits.
std::string status_display(const std::string& s) {
    static const std::map<std::string, std::string> kMap = {
        {"requested", "Requested"},   {"under_review", "Under Review"},
        {"approved", "Approved"},     {"packing", "Packing"},
        {"shipped", "Shipped"},       {"delivered", "Delivered"},
        {"cancelled", "Cancelled"},
    };
    auto it = kMap.find(s);
    return it != kMap.end() ? it->second : s;
}

}  // namespace

namespace api {

void register_routes(std::shared_ptr<Db> db, const Config& cfg) {
    using drogon::Get;
    using drogon::Post;
    auto& app = drogon::app();

    // Writes require the bearer token when one is configured. Without one the
    // service should only listen on localhost (the default B4P_HOST).
    auto authorize = [token = cfg.api_token](const HttpRequestPtr& req) {
        if (token.empty()) return;
        if (req->getHeader("authorization") != "Bearer " + token)
            throw HttpError(drogon::k401Unauthorized, "unauthorized",
                            "missing or wrong bearer token");
    };

    // ======================= health / status ================================

    app.registerHandler("/health",
        [db](const HttpRequestPtr&, Callback&& cb) {
            json j;
            try {
                auto row = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"health_counts"}).one_row();
                });
                j = {{"status", "ok"},
                     {"inventory_items", row[0].as<long>()},
                     {"supplies", row[1].as<long>()},
                     {"shipments", row[2].as<long>()},
                     {"db_pool", db->pool_size()}};
            } catch (const std::exception& e) {
                spdlog::error("/health: {}", e.what());
                j = {{"status", "degraded"}, {"error", e.what()}};
            }
            cb(json_resp(j));
        }, {Get});

    app.registerHandler("/kpis",
        [db](const HttpRequestPtr&, Callback&& cb) {
            handle("/kpis", cb, [&] {
                auto s = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"kpis"}).one_row()[0].as<std::string>();
                });
                return json_resp(json::parse(s));
            });
        }, {Get});

    app.registerHandler("/sync/status",
        [db](const HttpRequestPtr&, Callback&& cb) {
            handle("/sync/status", cb, [&] {
                auto r = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"sync_status"});
                });
                json last = r.empty() ? json(nullptr) : json::parse(r[0][0].as<std::string>());
                return json_resp({{"running", sync::running()}, {"last_run", last}});
            });
        }, {Get});

    app.registerHandler("/sync/run",
        [db, authorize](const HttpRequestPtr& req, Callback&& cb) {
            handle("/sync/run", cb, [&] {
                authorize(req);
                bool started = sync::run_cycle(db);
                return json_resp({{"started", started}},
                                 started ? drogon::k202Accepted : drogon::k409Conflict);
            });
        }, {Post});

    // ======================= dashboard-compatible reads =====================

    // No query string: the full inventory, byte-compatible with the Render
    // backend. Any of category / box / q / expiring_within / flagged /
    // limit / offset switches to the filtered query (same columns).
    app.registerHandler("/api/supplies",
        [db](const HttpRequestPtr& req, Callback&& cb) {
            handle("/api/supplies", cb, [&] {
                bool filtered = !req->getParameters().empty();
                auto category = param(req, "category");
                auto box = param(req, "box");
                auto q = param(req, "q");
                auto within = int_param(req, "expiring_within", 0, 36500);
                auto flagged = bool_param(req, "flagged");
                auto limit = int_param(req, "limit", 1, 100000);
                long offset = int_param(req, "offset", 0, LONG_MAX).value_or(0);
                auto rows = db->txn([&](pqxx::work& tx) {
                    if (!filtered) return tx.exec(pqxx::prepped{"supplies_all"});
                    return tx.exec(pqxx::prepped{"supplies_filtered"},
                                   pqxx::params{category, box, q, within, flagged, limit, offset});
                });
                json out = json::array();
                for (const auto& r : rows) {
                    json item;
                    for (const auto& f : r) item[f.name()] = field_json(f);
                    out.push_back(std::move(item));
                }
                return json_resp(out);
            });
        }, {Get});

    // Same shape as the Render backend, one JSON object per shipment line
    // item. "Review Flag" is not stored in the schema, so it is emitted as
    // the constant "FALSE" (see README).
    app.registerHandler("/requests",
        [db](const HttpRequestPtr&, Callback&& cb) {
            handle("/requests", cb, [&] {
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
                return json_resp(out);
            });
        }, {Get});

    // The Render backend's rule, verified against all live rows: reserved ->
    // "Requested" (tagged with the org), quantity <= 5 -> "Limited"/"Low
    // Stock", else "Available". Keyed by item_id (the backend uses sheet_row).
    app.registerHandler("/inventory/availability",
        [db](const HttpRequestPtr&, Callback&& cb) {
            handle("/inventory/availability", cb, [&] {
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
                return json_resp(out);
            });
        }, {Get});

    // ======================= expiration alerts ==============================

    // Expired + expiring within `days` (default 90), with bucket counts.
    app.registerHandler("/alerts/expiring",
        [db](const HttpRequestPtr& req, Callback&& cb) {
            handle("/alerts/expiring", cb, [&] {
                long days = int_param(req, "days", 0, 3650).value_or(90);
                auto rows = db->txn([&](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"expiring"}, pqxx::params{days});
                });
                json items = rows_typed(rows);
                json summary = {{"expired", 0}, {"30d", 0}, {"90d", 0}};
                for (const auto& it : items) summary[it["bucket"].get<std::string>()] =
                    summary[it["bucket"].get<std::string>()].get<int>() + 1;
                return json_resp({{"days", days}, {"summary", summary}, {"items", items}});
            });
        }, {Get});

    // ======================= review queue ===================================

    app.registerHandler("/review/queue",
        [db](const HttpRequestPtr& req, Callback&& cb) {
            handle("/review/queue", cb, [&] {
                long limit = int_param(req, "limit", 1, 500).value_or(50);
                long offset = int_param(req, "offset", 0, LONG_MAX).value_or(0);
                auto [rows, total] = db->txn([&](pqxx::work& tx) {
                    auto r = tx.exec(pqxx::prepped{"review_queue"}, pqxx::params{limit, offset});
                    long n = tx.exec(pqxx::prepped{"review_count"}).one_row()[0].as<long>();
                    return std::make_pair(std::move(r), n);
                });
                return json_resp({{"total", total}, {"items", rows_typed(rows)}});
            });
        }, {Get});

    // Resolve a flag by pointing the item at an existing catalog product.
    // body: {"supply_id": 123}
    app.registerHandler("/review/{1}/link",
        [db, authorize](const HttpRequestPtr& req, Callback&& cb, const std::string& item) {
            handle("/review/link", cb, [&] {
                authorize(req);
                long item_id = path_id(item, "item id");
                long supply_id = body_id(body_json(req), "supply_id");
                db->txn([&](pqxx::work& tx) {
                    if (tx.exec(pqxx::prepped{"supply_exists"}, pqxx::params{supply_id}).empty())
                        throw not_found("no supply " + std::to_string(supply_id));
                    if (tx.exec(pqxx::prepped{"review_link"}, pqxx::params{item_id, supply_id}).empty())
                        throw not_found("no active item " + std::to_string(item_id));
                });
                return json_resp({{"item_id", item_id}, {"supply_id", supply_id}});
            });
        }, {Post});

    // Resolve a flag by creating a new catalog product for it.
    // body: {"name": "...", "category_id": 3, "manufacturer": "...", "general_name": "..."}
    app.registerHandler("/review/{1}/new-supply",
        [db, authorize](const HttpRequestPtr& req, Callback&& cb, const std::string& item) {
            handle("/review/new-supply", cb, [&] {
                authorize(req);
                long item_id = path_id(item, "item id");
                json b = body_json(req);
                auto str = [&](const char* k) -> std::optional<std::string> {
                    auto it = b.find(k);
                    if (it == b.end() || it->is_null()) return std::nullopt;
                    if (!it->is_string()) throw bad_request(std::string(k) + " must be a string");
                    auto s = it->get<std::string>();
                    if (s.empty()) return std::nullopt;
                    return s;
                };
                auto name = str("name");
                if (!name) throw bad_request("name is required");
                long category = b.contains("category_id") ? body_id(b, "category_id") : 99;
                auto mfr = str("manufacturer");
                auto general = str("general_name");
                long supply_id = db->txn([&](pqxx::work& tx) {
                    std::optional<long> mid;
                    if (mfr) mid = tx.exec(pqxx::prepped{"mfr_upsert"}, pqxx::params{*mfr})
                                       .one_row()[0].as<long>();
                    long sid = tx.exec(pqxx::prepped{"supply_upsert"},
                                       pqxx::params{mid, category, general, *name})
                                   .one_row()[0].as<long>();
                    if (tx.exec(pqxx::prepped{"review_link"}, pqxx::params{item_id, sid}).empty())
                        throw not_found("no active item " + std::to_string(item_id));
                    return sid;
                });
                return json_resp({{"item_id", item_id}, {"supply_id", supply_id}},
                                 drogon::k201Created);
            });
        }, {Post});

    // ======================= catalog ========================================

    app.registerHandler("/catalog/duplicates",
        [db](const HttpRequestPtr& req, Callback&& cb) {
            handle("/catalog/duplicates", cb, [&] {
                long limit = int_param(req, "limit", 1, 1000).value_or(100);
                auto rows = db->txn([&](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"duplicates"}, pqxx::params{limit});
                });
                return json_resp(rows_typed(rows));
            });
        }, {Get});

    // body: {"keep": 12, "drop": 34} — moves drop's stock onto keep, deletes drop.
    app.registerHandler("/catalog/merge",
        [db, authorize](const HttpRequestPtr& req, Callback&& cb) {
            handle("/catalog/merge", cb, [&] {
                authorize(req);
                json b = body_json(req);
                long keep = body_id(b, "keep"), drop = body_id(b, "drop");
                long moved = db->txn([&](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"merge_supply"}, pqxx::params{keep, drop})
                        .one_row()[0].as<long>();
                });
                return json_resp({{"keep", keep}, {"dropped", drop}, {"items_moved", moved}});
            });
        }, {Post});

    // ======================= shipments / fulfillment ========================
    // {ref} is either the dashboard's Request ID (REQ-...) or a shipment_id.

    app.registerHandler("/shipments",
        [db](const HttpRequestPtr&, Callback&& cb) {
            handle("/shipments", cb, [&] {
                auto rows = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"shipments_list"});
                });
                return json_resp(rows_typed(rows));
            });
        }, {Get});

    app.registerHandler("/shipments/{1}",
        [db](const HttpRequestPtr&, Callback&& cb, const std::string& ref) {
            handle("/shipments/ref", cb, [&] {
                auto [lines, picks] = db->txn([&](pqxx::work& tx) {
                    long id = resolve_shipment(tx, ref);
                    return std::make_pair(
                        tx.exec(pqxx::prepped{"shipment_lines"}, pqxx::params{id}),
                        tx.exec(pqxx::prepped{"picklist"}, pqxx::params{id}));
                });
                return json_resp({{"lines", rows_typed(lines)}, {"reserved", rows_typed(picks)}});
            });
        }, {Get});

    app.registerHandler("/shipments/{1}/picklist",
        [db](const HttpRequestPtr&, Callback&& cb, const std::string& ref) {
            handle("/shipments/picklist", cb, [&] {
                auto rows = db->txn([&](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"picklist"},
                                   pqxx::params{resolve_shipment(tx, ref)});
                });
                return json_resp(rows_typed(rows));
            });
        }, {Get});

    app.registerHandler("/shipments/{1}/reserve",
        [db, authorize](const HttpRequestPtr& req, Callback&& cb, const std::string& ref) {
            handle("/shipments/reserve", cb, [&] {
                authorize(req);
                auto rows = db->txn([&](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"reserve"},
                                   pqxx::params{resolve_shipment(tx, ref)});
                });
                return json_resp({{"reserved", rows_typed(rows)}});
            });
        }, {Post});

    app.registerHandler("/shipments/{1}/release",
        [db, authorize](const HttpRequestPtr& req, Callback&& cb, const std::string& ref) {
            handle("/shipments/release", cb, [&] {
                authorize(req);
                long n = db->txn([&](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"release"},
                                   pqxx::params{resolve_shipment(tx, ref)})
                        .one_row()[0].as<long>();
                });
                return json_resp({{"released", n}});
            });
        }, {Post});

    // ======================= boxes ==========================================

    app.registerHandler("/boxes",
        [db](const HttpRequestPtr&, Callback&& cb) {
            handle("/boxes", cb, [&] {
                auto rows = db->txn([](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"boxes_all"});
                });
                return json_resp(rows_typed(rows));
            });
        }, {Get});

    // /boxes/contents?label=TC/RC-11 (query param because labels contain '/')
    app.registerHandler("/boxes/contents",
        [db](const HttpRequestPtr& req, Callback&& cb) {
            handle("/boxes/contents", cb, [&] {
                auto label = param(req, "label");
                if (!label) throw bad_request("label is required");
                auto rows = db->txn([&](pqxx::work& tx) {
                    return tx.exec(pqxx::prepped{"box_contents"}, pqxx::params{*label});
                });
                return json_resp(rows_typed(rows));
            });
        }, {Get});
}

}  // namespace api
