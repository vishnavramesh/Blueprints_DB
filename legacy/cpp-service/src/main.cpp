// b4p-service — C++ inventory service for the Blueprints for Pangaea database.
//
// One process that serves the inventory over HTTP (api.cpp), keeps local
// Postgres in step with the dashboard feeds (sync.cpp), and serves the small
// operations UI in web/ (review queue, expiring stock, shipments, labels).
//
// Build:   cmake -B build && cmake --build build
// Run:     ./build/b4p-service          (settings: see config.h / README)
// Try:     curl localhost:8080/health   ·   open http://localhost:8080/

#include <drogon/drogon.h>
#include <spdlog/spdlog.h>

#include "api.h"
#include "config.h"
#include "db.h"
#include "sync.h"

int main() {
    Config cfg;
    try {
        cfg = Config::from_env();
    } catch (const std::exception& e) {
        spdlog::critical("bad B4P_* environment variable: {}", e.what());
        return 2;
    }
    if (std::getenv("B4P_DEBUG"))
        spdlog::set_level(spdlog::level::debug);
    spdlog::info("b4p-service starting on {}:{} (db pool {}, {} threads)",
                 cfg.host, cfg.port, cfg.db_pool, cfg.threads);
    if (cfg.api_token.empty() && cfg.host != "127.0.0.1" && cfg.host != "localhost")
        spdlog::warn("listening on {} with no B4P_API_TOKEN: anyone who can reach this "
                     "port can reserve stock and merge catalog entries", cfg.host);

    // Db is created before the server starts: if Postgres is down we exit
    // immediately with a clear error instead of serving 500s.
    std::shared_ptr<Db> db;
    try {
        db = std::make_shared<Db>(cfg.db_conninfo, static_cast<size_t>(cfg.db_pool));
    } catch (const std::exception& e) {
        spdlog::critical("cannot connect to postgres ({}): {}", cfg.db_conninfo, e.what());
        return 1;
    }

    api::register_routes(db, cfg);
    sync::start(db, cfg.feed_host, cfg.sync_every);

    // Graceful shutdown: SIGTERM (launchctl stop) / SIGINT (Ctrl-C) stop the
    // loops; a sync already inside its transaction finishes first because it
    // runs on the main loop that quit() is queued on.
    auto quit = [] {
        spdlog::info("shutting down");
        drogon::app().quit();
    };
    drogon::app()
        .setTermSignalHandler(quit)
        .setIntSignalHandler(quit)
        .setThreadNum(static_cast<size_t>(cfg.threads))
        .setDocumentRoot(cfg.web_root)
        .setHomePage("index.html")
        .addListener(cfg.host, static_cast<uint16_t>(cfg.port))
        .run();
    spdlog::info("stopped");
    return 0;
}
