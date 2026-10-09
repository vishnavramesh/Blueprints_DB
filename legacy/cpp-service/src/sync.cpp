#include "sync.h"

#include <algorithm>
#include <atomic>
#include <functional>
#include <map>
#include <optional>
#include <set>
#include <string>

#include <drogon/drogon.h>
#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>

#include "db.h"
#include "normalize.h"
#include "rowhash.h"

using nlohmann::json;

namespace {

constexpr auto kSuppliesPath = "/api/supplies";
constexpr auto kRequestsPath = "/requests";
constexpr double kFetchTimeoutSec = 90.0;  // matches monitor.py's `curl -m 90`

// A feed that suddenly has far fewer rows than we hold is far more likely a
// broken backend than a mass deletion; below this fraction, removals are
// skipped (and a warning logged) instead of hiding most of the inventory.
constexpr double kMinFeedFraction = 0.5;

std::string g_feed_host;

// str(row.get(key) or "") — the feed is sheet-backed so values are strings,
// but be tolerant of numbers/bools/null exactly like Python's str().
std::string text(const json& row, const char* key) {
    auto it = row.find(key);
    if (it == row.end() || it->is_null()) return "";
    std::string s = it->is_string() ? it->get<std::string>() : it->dump();
    // Postgres text can't hold NUL and libpq would silently truncate at it.
    s.erase(std::remove(s.begin(), s.end(), '\0'), s.end());
    return s;
}

std::optional<std::string> opt(const std::string& s) {
    std::string t = normalize::strip(s);
    if (t.empty()) return std::nullopt;
    return t;
}

struct Stats {
    int items_added = 0, items_updated = 0, items_removed = 0, rows_skipped = 0;
    int requests_added = 0, lines_added = 0, status_changes = 0;
};

normalize::Rules load_rules(pqxx::transaction_base& tx) {
    normalize::Rules r;
    for (auto row : tx.exec(pqxx::prepped{"rules_categories"}))
        r.categories[row[0].as<std::string>()] = row[1].as<long>();
    for (auto row : tx.exec(pqxx::prepped{"rules_mfr"}))
        r.mfr_aliases[row[0].as<std::string>()] = row[1].as<std::string>();
    for (auto row : tx.exec(pqxx::prepped{"rules_box_types"}))
        r.box_types.insert(row[0].as<std::string>());
    for (auto row : tx.exec(pqxx::prepped{"rules_box_fix"}))
        r.box_type_fix[row[0].as<std::string>()] = row[1].as<std::string>();
    for (auto row : tx.exec(pqxx::prepped{"rules_status"}))
        r.statuses[row[0].as<std::string>()] = row[1].as<std::string>();
    return r;
}

// ---------------------------------------------------------------------------
// Supplies
// ---------------------------------------------------------------------------

struct Known { long item_id; std::string hash; long qty; bool removed; };

// Fields of one feed row, normalized and with catalog/box ids resolved.
struct ItemFields {
    std::optional<long> supply_id, box_id;
    std::optional<std::string> lot, expiration, raw_name, image_url, notes;
    bool flagged = false;
    long qty = 0;
};

ItemFields resolve(pqxx::transaction_base& tx, const json& row, const std::string& name,
                   const normalize::Rules& rules) {
    ItemFields f;
    // Unreadable AI scans ('{surgical scissors}') and rows marked for review
    // stay out of the catalog: flagged + raw_name, no supply row.
    std::string review = normalize::strip(text(row, "Review"));
    f.flagged = name[0] == '{' || !(review.empty() || review == "No review needed");
    if (f.flagged) {
        f.raw_name = name;
    } else {
        std::optional<long> mid;
        if (auto mfr = normalize::norm_mfr(text(row, "Manufacturer Name"), rules))
            mid = tx.exec(pqxx::prepped{"mfr_upsert"}, pqxx::params{*mfr})
                    .one_row()[0].as<long>();
        f.supply_id = tx.exec(pqxx::prepped{"supply_upsert"},
                pqxx::params{mid, normalize::norm_category(text(row, "Category"), rules),
                             opt(text(row, "General")), name})
                .one_row()[0].as<long>();
    }
    if (auto box = normalize::parse_box(text(row, "Box Number"), rules))
        f.box_id = tx.exec(pqxx::prepped{"box_upsert"},
                pqxx::params{box->first, box->second,
                             normalize::parse_pallet(text(row, "Pallet Number"))})
                .one_row()[0].as<long>();
    std::string lot = normalize::strip(text(row, "Lot Number"));
    if (!lot.empty() && normalize::lower(lot) != "unspecified") f.lot = lot;
    f.expiration = normalize::parse_date(text(row, "Date of Expiration"));
    f.image_url = opt(text(row, "Image URL"));
    f.notes = opt(text(row, "Notes"));
    f.qty = normalize::parse_qty(text(row, "Quantity"));
    return f;
}

void ledger(pqxx::transaction_base& tx, long item_id, const char* type, long delta,
            const std::string& note) {
    if (delta == 0) return;
    tx.exec(pqxx::prepped{"txn_insert"},
            pqxx::params{item_id, type, delta, note, "sync"});
}

void sync_supplies(pqxx::work& tx, const json& rows, const normalize::Rules& rules,
                   Stats& st) {
    std::map<std::string, Known> known;
    size_t active = 0;
    for (auto r : tx.exec(pqxx::prepped{"known_items"})) {
        Known k{r[1].as<long>(), r[2].is_null() ? "" : r[2].as<std::string>(),
                r[3].as<long>(), r[4].as<bool>()};
        if (!k.removed) active++;
        known.emplace(r[0].as<std::string>(), std::move(k));
    }

    std::set<std::string> seen;
    for (const auto& row : rows) {
        if (!row.is_object()) { st.rows_skipped++; continue; }
        std::string name = normalize::strip(text(row, "Name"));
        std::string h = rowhash::row_hash(row);
        std::string key = opt(text(row, "Image URL")).value_or("hash:" + h);
        if (!seen.insert(key).second) {
            spdlog::warn("SKIP duplicate row key {} ('{}')", key, name.substr(0, 40));
            st.rows_skipped++;
            continue;
        }
        if (name.empty()) continue;

        auto it = known.find(key);
        if (it != known.end() && it->second.hash == h && !it->second.removed)
            continue;  // unchanged

        // Savepoint per row: a row Postgres rejects is logged and skipped
        // instead of aborting the whole cycle's transaction.
        try {
            pqxx::subtransaction sub(tx, "row");
            ItemFields f = resolve(sub, row, name, rules);
            if (it == known.end()) {
                std::optional<long> days;
                if (auto d = opt(text(row, "Days in Warehouse"));
                    d && d->find_first_not_of("0123456789") == std::string::npos &&
                    d->size() <= 5)
                    days = std::stol(*d);
                long id = sub.exec(pqxx::prepped{"item_insert"},
                        pqxx::params{f.supply_id, f.box_id, f.lot, f.expiration, f.flagged,
                                     f.raw_name, f.image_url, f.notes, h, key, days})
                        .one_row()[0].as<long>();
                ledger(sub, id, "received", f.qty, "new row in dashboard feed");
                st.items_added++;
                spdlog::info("NEW item in: '{}' qty={} box={}{}", name.substr(0, 60), f.qty,
                             text(row, "Box Number").empty() ? "-" : text(row, "Box Number"),
                             f.flagged ? " FLAGGED" : "");
            } else {
                const Known& k = it->second;
                sub.exec(pqxx::prepped{"item_update"},
                         pqxx::params{k.item_id, f.supply_id, f.box_id, f.lot, f.expiration,
                                      f.flagged, f.raw_name, f.image_url, f.notes, h});
                ledger(sub, k.item_id, "adjusted", f.qty - k.qty,
                       k.removed ? "reappeared in dashboard feed" : "edited in dashboard");
                st.items_updated++;
                spdlog::info("CHANGE item {} '{}'{}{}", k.item_id, name.substr(0, 60),
                             f.qty != k.qty ? fmt::format(" qty {}->{}", k.qty, f.qty) : "",
                             k.removed ? " (reappeared)" : "");
            }
            sub.commit();
        } catch (const std::exception& e) {
            st.rows_skipped++;
            spdlog::error("SKIP row '{}': {}", name.substr(0, 60), e.what());
        }
    }

    // Rows that disappeared from the feed.
    if (active > 0 && seen.size() < kMinFeedFraction * static_cast<double>(active)) {
        spdlog::warn("supplies feed has {} rows but {} are active locally; "
                     "not treating the missing ones as removed", seen.size(), active);
        return;
    }
    for (const auto& [key, k] : known) {
        if (k.removed || seen.count(key)) continue;
        tx.exec(pqxx::prepped{"item_remove"}, pqxx::params{k.item_id});
        st.items_removed++;
        spdlog::info("REMOVED item {} (no longer in dashboard feed)", k.item_id);
    }
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

void sync_requests(pqxx::work& tx, const json& rows, const normalize::Rules& rules,
                   Stats& st) {
    // Group line items by Request ID, keeping feed order within each request.
    std::vector<std::string> order;
    std::map<std::string, std::vector<const json*>> groups;
    for (const auto& row : rows) {
        if (!row.is_object()) continue;
        std::string org = normalize::strip(text(row, "Org Name"));
        std::string rid = normalize::strip(text(row, "Request ID"));
        if (org.empty() || rid.empty()) continue;
        auto [it, fresh] = groups.try_emplace(rid);
        if (fresh) order.push_back(rid);
        it->second.push_back(&row);
    }

    for (const auto& rid : order) {
        const auto& lines = groups[rid];
        const json& head = *lines.front();
        std::string org = normalize::strip(text(head, "Org Name"));
        try {
            pqxx::subtransaction sub(tx, "request");
            long qid = sub.exec(pqxx::prepped{"requester_upsert"},
                    pqxx::params{org, opt(text(head, "Org Email"))})
                    .one_row()[0].as<long>();
            std::string status = normalize::map_status(text(head, "Status"), rules);
            auto hit = sub.exec(pqxx::prepped{"shipment_by_req"}, pqxx::params{rid});
            long sid;
            bool is_new = hit.empty();
            if (is_new) {
                sid = sub.exec(pqxx::prepped{"shipment_insert"},
                        pqxx::params{qid, rid, status,
                                     normalize::parse_timestamp(text(head, "Timestamp"))})
                        .one_row()[0].as<long>();
                st.requests_added++;
                spdlog::info("NEW request {} from '{}' ({} line{})", rid, org, lines.size(),
                             lines.size() == 1 ? "" : "s");
            } else {
                sid = hit[0][0].as<long>();
                std::string old = hit[0][1].as<std::string>();
                if (old != status) {
                    sub.exec(pqxx::prepped{"shipment_status_update"}, pqxx::params{status, sid});
                    st.status_changes++;
                    spdlog::info("CHANGE request {} ({}): {} -> {}", rid, org, old, status);
                }
            }
            for (size_t i = 0; i < lines.size(); i++) {
                const json& row = *lines[i];
                long q = normalize::parse_qty(text(row, "Quantity Requested"));
                std::string item = normalize::strip(text(row, "Item Name"));
                auto r = sub.exec(pqxx::prepped{"shipment_item_upsert"},
                        pqxx::params{sid, static_cast<long>(i + 1), item.empty() ? "?" : item,
                                     normalize::norm_category(text(row, "Category"), rules),
                                     q ? std::optional<long>(q) : std::nullopt});
                if (!r.empty() && r[0][0].as<bool>()) {
                    st.lines_added++;
                    if (!is_new)
                        spdlog::info("NEW line {} on request {}: {}", i + 1, rid, item.substr(0, 50));
                }
            }
            sub.commit();
        } catch (const std::exception& e) {
            st.rows_skipped++;
            spdlog::error("SKIP request {}: {}", rid, e.what());
        }
    }
}

// ---------------------------------------------------------------------------
// Consistency checks. Each runs in its OWN transaction: a failed statement
// poisons a transaction, and one broken check must not take the others down.
// Findings are logged only when they change, so a steady state doesn't write
// the same four warnings every five minutes.
// ---------------------------------------------------------------------------

const std::pair<const char*, const char*> kChecks[] = {
    {"negative stock (ledger pushed quantity below zero)",
     "SELECT count(*) FROM inventory_item WHERE quantity < 0 HAVING count(*) > 0"},
    {"items reserved for a cancelled/delivered shipment",
     "SELECT count(*) FROM inventory_item i JOIN shipment s ON s.shipment_id = i.reserved_for "
     "WHERE s.status IN ('cancelled','delivered') AND i.removed_at IS NULL HAVING count(*) > 0"},
    {"expired stock not yet flagged or reserved",
     "SELECT count(*) FROM inventory_item WHERE expiration_date < CURRENT_DATE "
     "AND NOT flagged AND reserved_for IS NULL AND removed_at IS NULL HAVING count(*) > 0"},
    {"likely duplicate catalog entries",
     "SELECT count(*) FROM v_duplicate_supplies HAVING count(*) > 0"},
    {"inventory items with zero quantity still in a box",
     "SELECT count(*) FROM inventory_item WHERE quantity = 0 AND box_id IS NOT NULL "
     "AND removed_at IS NULL HAVING count(*) > 0"},
    {"flagged items awaiting review",
     "SELECT count(*) FROM v_review_queue HAVING count(*) > 0"},
};

std::map<std::string, std::string> g_last_findings;  // main loop only

json run_checks(Db& db) {
    json findings = json::object();
    for (const auto& [label, sql] : kChecks) {
        std::string detail;
        try {
            auto rows = db.txn([sql = sql](pqxx::work& tx) { return tx.exec(sql); }, "sync");
            if (rows.empty()) continue;
            detail = rows[0][0].as<std::string>();
            findings[label] = rows[0][0].as<long>();
        } catch (const std::exception& e) {
            detail = std::string("check failed: ") + e.what();
            findings[label] = detail;
        }
        auto prev = g_last_findings.find(label);
        if (prev == g_last_findings.end() || prev->second != detail)
            spdlog::warn("{}: {}", label, detail);
    }
    std::map<std::string, std::string> now;
    for (auto& [k, v] : findings.items())
        now[k] = v.is_string() ? v.get<std::string>() : v.dump();
    for (const auto& [k, _] : g_last_findings)
        if (!now.count(k)) spdlog::info("resolved: {}", k);
    g_last_findings = std::move(now);
    return findings;
}

struct Counts { long items, supplies, shipments, units; };

Counts counts(Db& db) {
    auto r = db.txn([](pqxx::work& tx) {
        return tx.exec(pqxx::prepped{"sync_counts"}).one_row();
    }, "sync");
    return {r[0].as<long>(), r[1].as<long>(), r[2].as<long>(), r[3].as<long>()};
}

// ---------------------------------------------------------------------------
// The async cycle. drogon::HttpClient callbacks fire on the loop the client
// was created on (the main loop here), so the whole chain —
// fetch supplies -> fetch requests -> one write transaction -> checks —
// runs sequentially on that loop.
// ---------------------------------------------------------------------------

std::atomic<bool> g_running{false};

void fetch(const std::string& path, std::function<void(std::optional<json>)> done) {
    auto client = drogon::HttpClient::newHttpClient(g_feed_host, drogon::app().getLoop());
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
                auto j = json::parse(resp->body());
                if (!j.is_array()) throw std::runtime_error("expected a JSON array");
                done(std::move(j));
            } catch (const std::exception& e) {
                spdlog::error("fetch {}: bad JSON: {}", path, e.what());
                done(std::nullopt);
            }
        },
        kFetchTimeoutSec);
}

void finish_cycle(std::shared_ptr<Db> db,
                  std::optional<json> supplies, std::optional<json> requests) {
    Stats st;
    std::optional<long> run_id;
    std::optional<std::string> error;
    json findings = json::object();
    try {
        run_id = db->txn([](pqxx::work& tx) {
            return tx.exec(pqxx::prepped{"sync_run_start"}).one_row()[0].as<long>();
        }, "sync");
        Counts before = counts(*db);
        db->txn([&](pqxx::work& tx) {
            normalize::Rules rules;
            try {
                pqxx::subtransaction sub(tx, "rules");
                rules = load_rules(sub);
                sub.commit();
            } catch (const std::exception& e) {
                spdlog::error("rule tables unreadable, using built-in rules: {}", e.what());
                rules = normalize::Rules::defaults();
            }
            if (supplies) sync_supplies(tx, *supplies, rules, st);
            if (requests) sync_requests(tx, *requests, rules, st);
        }, "sync");
        Counts after = counts(*db);
        if (st.items_added || st.items_updated || st.items_removed || st.requests_added ||
            st.lines_added || st.status_changes)
            spdlog::info("SYNC items +{} ~{} -{}, requests +{} (lines +{}, {} status changes) "
                         "(items {}->{}, units {}->{})",
                         st.items_added, st.items_updated, st.items_removed, st.requests_added,
                         st.lines_added, st.status_changes, before.items, after.items,
                         before.units, after.units);
        else
            spdlog::debug("OK in sync: {} items / {} units / {} shipments",
                          after.items, after.units, after.shipments);
        if (st.rows_skipped) spdlog::warn("{} feed rows skipped (see errors above)", st.rows_skipped);
        findings = run_checks(*db);
    } catch (const std::exception& e) {
        error = e.what();
        spdlog::error("sync cycle failed: {}", e.what());
    }
    if (run_id) {
        try {
            db->txn([&](pqxx::work& tx) {
                tx.exec(pqxx::prepped{"sync_run_finish"},
                        pqxx::params{*run_id, st.items_added, st.items_updated,
                                     st.items_removed, st.rows_skipped, st.requests_added,
                                     st.lines_added, st.status_changes,
                                     supplies.has_value(), requests.has_value(),
                                     findings.dump(), error});
            }, "sync");
        } catch (const std::exception& e) {
            spdlog::error("could not record sync_run {}: {}", *run_id, e.what());
        }
    }
    g_running = false;
}

}  // namespace

namespace sync {

bool running() { return g_running; }

bool run_cycle(std::shared_ptr<Db> db) {
    bool expected = false;
    if (!g_running.compare_exchange_strong(expected, true)) {
        spdlog::warn("sync cycle skipped: previous cycle still running");
        return false;
    }
    // Hop onto the main loop: the HttpClients and g_last_findings live there.
    drogon::app().getLoop()->queueInLoop([db] {
        fetch(kSuppliesPath, [db](std::optional<json> supplies) {
            fetch(kRequestsPath, [db, supplies = std::move(supplies)](
                                     std::optional<json> requests) {
                finish_cycle(db, supplies, std::move(requests));
            });
        });
    });
    return true;
}

void start(std::shared_ptr<Db> db, const std::string& feed_host, double interval_sec) {
    g_feed_host = feed_host;
    drogon::app().getLoop()->runAfter(1.0, [db] { run_cycle(db); });
    if (interval_sec > 0) {
        drogon::app().getLoop()->runEvery(interval_sec, [db] { run_cycle(db); });
        spdlog::info("sync scheduled every {}s against {}", interval_sec, feed_host);
    } else {
        spdlog::info("periodic sync disabled; POST /sync/run to sync against {}", feed_host);
    }
}

}  // namespace sync
