#pragma once
// sync.h — keeps local Postgres in step with the dashboard feeds.
//
// One "cycle" = fetch both dashboard feeds, apply them inside ONE transaction
// (each row in its own savepoint, so one bad row is skipped instead of
// rolling back the cycle), then run the consistency checks. Everything runs
// on Drogon's main event loop: the HTTP fetches are async (drogon::HttpClient)
// and the database work happens in the fetch completion callbacks.
//
// Row identity (migration 001): a supplies row is identified by its Image URL
// (source_key), and its md5 (source_hash) only detects edits. So:
//   unseen key            -> INSERT + 'received' ledger row
//   known key, new hash   -> UPDATE in place + 'adjusted' ledger row for the
//                            quantity delta (a dashboard edit)
//   known key missing     -> removed_at = now() (hidden from views, kept for
//                            history) — skipped if the feed looks truncated
// Requests are keyed by (Request ID, position within the request), so every
// line of a multi-line request is stored.

#include <memory>
#include <string>

class Db;

namespace sync {

// Register the recurring sync on Drogon's main loop (call before app().run()).
// interval_sec <= 0 disables the timer (cycles then only run on demand).
// Always schedules one cycle shortly after startup.
void start(std::shared_ptr<Db> db, const std::string& feed_host, double interval_sec);

// Run a single cycle now (asynchronous; returns immediately; safe to call
// from any thread). Returns false if a cycle is already in flight.
bool run_cycle(std::shared_ptr<Db> db);

bool running();

}  // namespace sync
