#pragma once
// config.h — runtime settings from environment variables (with defaults that
// reproduce the old hard-coded constants). Env vars rather than a file so the
// launchd plist, the tests and CI can all set them the same way.
//
//   B4P_DB          libpq conninfo                 "dbname=b4p"
//   B4P_HOST        listen address                 "127.0.0.1"
//   B4P_PORT        listen port                    8080
//   B4P_THREADS     HTTP worker threads            4
//   B4P_DB_POOL     Postgres connections           4
//   B4P_FEED_HOST   dashboard backend to sync from "https://b4p-external-view-w26-backend.onrender.com"
//   B4P_SYNC_EVERY  seconds between syncs; 0 = only on POST /sync/run   300
//   B4P_API_TOKEN   if set, POST endpoints require "Authorization: Bearer <token>"
//   B4P_WEB_ROOT    directory served at /ui/       <source>/web

#include <cstdlib>
#include <string>

#ifndef B4P_DEFAULT_WEB_ROOT
#define B4P_DEFAULT_WEB_ROOT "web"
#endif

struct Config {
    std::string db_conninfo = "dbname=b4p";
    std::string host        = "127.0.0.1";
    int         port        = 8080;
    int         threads     = 4;
    int         db_pool     = 4;
    std::string feed_host   = "https://b4p-external-view-w26-backend.onrender.com";
    double      sync_every  = 300.0;
    std::string api_token;
    std::string web_root    = B4P_DEFAULT_WEB_ROOT;

    static Config from_env() {
        Config c;
        auto str = [](const char* k, std::string& out) {
            if (const char* v = std::getenv(k); v && *v) out = v;
        };
        auto num = [](const char* k, auto& out) {
            if (const char* v = std::getenv(k); v && *v) out = static_cast<std::decay_t<decltype(out)>>(std::stod(v));
        };
        str("B4P_DB", c.db_conninfo);
        str("B4P_HOST", c.host);
        num("B4P_PORT", c.port);
        num("B4P_THREADS", c.threads);
        num("B4P_DB_POOL", c.db_pool);
        str("B4P_FEED_HOST", c.feed_host);
        num("B4P_SYNC_EVERY", c.sync_every);
        str("B4P_API_TOKEN", c.api_token);
        str("B4P_WEB_ROOT", c.web_root);
        return c;
    }
};
