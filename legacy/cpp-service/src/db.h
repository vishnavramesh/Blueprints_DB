#pragma once
// db.h — thin ownership layer over libpqxx.
//
// Design notes (why this file exists at all):
//  * Drogon handlers run on multiple event-loop threads, but a pqxx::connection
//    is NOT thread-safe. Db owns a small pool: txn() checks a connection out
//    (blocking if all are busy), runs the caller's lambda in a transaction, and
//    checks it back in. A connection is only ever used by one thread at a time.
//  * All SQL lives behind named prepared statements registered in one place
//    (Db::prepare_all). Never string-concatenate SQL into a query — libpqxx
//    parameterization ($1, $2...) is the only injection-safe path.
//  * Every transaction tags itself with `b4p.source` (read by the audit_log
//    trigger from migration 001), so the audit trail says who made a change.

#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <pqxx/pqxx>

class Db {
public:
    // conninfo example: "dbname=b4p" (local socket, current user).
    // For a remote DB: "host=... port=5432 dbname=b4p user=... password=..."
    // The first connection is opened here, so an unreachable DB fails fast.
    Db(const std::string& conninfo, size_t pool_size);

    // Run `fn` inside a transaction. Commits on normal return, rolls back if
    // fn throws. This is the ONLY way callers touch the database — it makes
    // "every operation is atomic" a structural guarantee instead of a habit.
    //
    // Usage:
    //   auto rows = db.txn([](pqxx::work& tx) {
    //       return tx.exec(pqxx::prepped{"supplies_all"});
    //   });
    template <typename Fn>
    auto txn(Fn&& fn, const char* source = "api") {
        Lease lease(*this);
        try {
            pqxx::work tx(*lease.conn);
            tx.exec("SELECT set_config('b4p.source', $1, true)", pqxx::params{source});
            if constexpr (std::is_void_v<decltype(fn(tx))>) {
                fn(tx);
                tx.commit();
            } else {
                auto result = fn(tx);
                tx.commit();
                return result;
            }
        } catch (const pqxx::broken_connection&) {
            lease.broken = true;  // don't return a dead connection to the pool
            throw;
        }
    }

    size_t pool_size() const { return size_; }

private:
    using ConnPtr = std::unique_ptr<pqxx::connection>;

    // RAII checkout: returns the connection to the pool (or drops it if
    // broken) no matter how the transaction ends.
    struct Lease {
        explicit Lease(Db& db);
        ~Lease();
        Db& db;
        ConnPtr conn;
        bool broken = false;
    };

    ConnPtr open();                           // connect + prepare_all
    static void prepare_all(pqxx::connection& c);  // every named statement, one place

    std::string conninfo_;
    size_t size_;
    size_t created_ = 0;                      // live connections (idle + leased)
    std::vector<ConnPtr> idle_;
    std::mutex mu_;
    std::condition_variable cv_;
};
