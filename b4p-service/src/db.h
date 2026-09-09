#pragma once
// db.h — thin ownership layer over libpqxx.
//
// Design notes (why this file exists at all):
//  * Drogon handlers run on multiple event-loop threads, but a pqxx::connection
//    is NOT thread-safe. So each request either takes a pooled connection or
//    (milestone 1) we serialize access with a mutex. The pool upgrade is a
//    MILESTONE 5 task — the interface below won't need to change, only the
//    internals. That's the point of wrapping early.
//  * All SQL lives behind named prepared statements registered in one place
//    (Db::prepare_all). Never string-concatenate SQL into a query — libpqxx
//    parameterization ($1, $2...) is the only injection-safe path.

#include <mutex>
#include <string>
#include <pqxx/pqxx>

class Db {
public:
    // conninfo example: "dbname=b4p" (local socket, current user).
    // For a remote DB later: "host=... port=5432 dbname=b4p user=... password=..."
    explicit Db(const std::string& conninfo);

    // Run `fn` inside a transaction. Commits on normal return, rolls back if
    // fn throws. This is the ONLY way callers touch the database — it makes
    // "every operation is atomic" a structural guarantee instead of a habit.
    //
    // Usage:
    //   auto rows = db.txn([](pqxx::work& tx) {
    //       return tx.exec_prepared("supplies_all");
    //   });
    template <typename Fn>
    auto txn(Fn&& fn) {
        std::lock_guard<std::mutex> lock(mu_);   // MILESTONE 5: replace with pool checkout
        ensure_open();
        pqxx::work tx(*conn_);
        if constexpr (std::is_void_v<decltype(fn(tx))>) {
            fn(tx);
            tx.commit();
        } else {
            auto result = fn(tx);
            tx.commit();
            return result;
        }
    }

private:
    void ensure_open();       // (re)connects and re-registers prepared statements
    void prepare_all();       // every named statement, one place

    std::string conninfo_;
    std::unique_ptr<pqxx::connection> conn_;
    std::mutex mu_;
};
