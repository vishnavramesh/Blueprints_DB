#pragma once
// sync.h — MILESTONE 4: the C++ port of monitor.py.
//
// One "cycle" = fetch both dashboard feeds, insert every row whose
// source_hash is unseen inside ONE transaction, then run the consistency
// checks. Everything runs on Drogon's main event loop: the HTTP fetches are
// async (drogon::HttpClient), and the database work happens in the fetch
// completion callbacks, so no extra thread is ever created — Db's mutex is
// the only synchronization in the whole service.

#include <memory>

class Db;

namespace sync {

// Register the recurring sync on Drogon's main loop (call before app().run()).
// Also schedules one cycle shortly after startup so the service is current
// immediately instead of five minutes later.
void start(std::shared_ptr<Db> db, double interval_sec);

// Run a single cycle now (asynchronous; returns immediately). Skips silently
// if a previous cycle is still in flight.
void run_cycle(std::shared_ptr<Db> db);

}  // namespace sync
