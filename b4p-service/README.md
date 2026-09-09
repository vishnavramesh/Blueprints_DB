# b4p-service

A single-binary C++ service for the Blueprints for Pangaea inventory database.
It replaces the `monitor.py` terminal loop and re-serves the Render backend's
API straight from local Postgres: one process that **serves** the inventory
over HTTP and, every five minutes, **syncs** new rows from the live dashboard
feeds and runs consistency checks.

```
                        ┌──────────────────────────────────────────────┐
   Render backend       │  b4p-service (one process)                   │
   (live feeds)         │                                              │
  /api/supplies ──────► │  sync.cpp ── every 300 s ──┐                 │
  /requests     ──────► │  (drogon::HttpClient,      │  one txn        │
                        │   async, main loop)        ▼                 │
                        │                        db.cpp ────────► Postgres (b4p)
   dashboard / curl     │                            ▲                 │
  /health       ──────► │  main.cpp handlers ────────┘                 │
  /api/supplies ──────► │  (drogon worker threads,   normalize.cpp    │
  /requests     ──────► │   prepared statements)     (pure functions)  │
  /inventory/availability ─►                                           │
                        └──────────────────────────────────────────────┘
```

## Build and run

```sh
brew install drogon nlohmann-json spdlog libpqxx   # deps (macOS / Homebrew)
cmake -B build && cmake --build build
ctest --test-dir build          # unit tests (normalize rules)
./build/b4p-service             # listens on :8080, connects to dbname=b4p
curl localhost:8080/health
```

The binary connects to Postgres **before** the server starts — if the DB is
unreachable it exits immediately with a clear error instead of serving 500s.
If Postgres restarts underneath a running service, the next query notices the
dead connection and reconnects lazily (`Db::ensure_open`), so a
`brew services restart postgresql@16` needs no service restart.

## Source layout

| File | Role |
|---|---|
| `src/main.cpp` | wiring: HTTP route handlers, sync scheduling, config constants |
| `src/db.h/.cpp` | ownership of the Postgres connection; all SQL as named prepared statements |
| `src/normalize.h/.cpp` | pure normalization rules ported from `load.py` (no I/O, fully unit-tested) |
| `src/sync.h/.cpp` | the `monitor.py` port: feed fetch, row hashing, sync, consistency checks |
| `tests/normalize_test.cpp` | pins the real-world edge cases (`"1 (full box)"`, `"TC/RC 11"`, unicode dashes, …) |

## Threading model (how the "multithreaded" part actually works)

Drogon runs a **main event loop** plus a pool of **worker event-loop
threads**; HTTP handlers may fire on any of them concurrently. The design
keeps the concurrency surface deliberately tiny:

* **`Db` is the only shared mutable state.** A `pqxx::connection` is *not*
  thread-safe, so `Db::txn()` takes a `std::mutex` before touching it. Every
  database operation in the entire service goes through `txn()`, which opens a
  `pqxx::work`, runs the caller's lambda, and commits on normal return /
  rolls back on throw. Atomicity is a structural guarantee, not a habit.
* **The sync loop adds no threads.** `sync::start()` hangs a
  `runEvery(300 s)` timer off Drogon's *main* loop. The feed fetches use
  `drogon::HttpClient`, which is asynchronous: its completion callbacks fire
  back on the same main loop, where the database writes then happen (behind
  the same `Db` mutex as the handlers). An `std::atomic<bool>` guard skips a
  cycle if the previous one is somehow still in flight.
* **`normalize` is pure.** Every function is `(string) -> value` with no
  state, so it is trivially safe from any thread.

Net effect: request handlers on worker threads and the sync cycle on the main
loop can interleave freely; the one mutex inside `Db` serializes their actual
database access. Upgrading to a connection pool (milestone 5) only changes
`Db`'s internals — the `txn()` interface stays the same, which is why the
wrapper exists.

## HTTP endpoints

All responses are JSON. Errors return `{"error": ...}` with status 500
(`/health` instead reports `"status": "degraded"` with a 200).

* **`GET /health`** — row counts from `inventory_item` / `supply` /
  `shipment`; proves DB connectivity.
* **`GET /api/supplies`** — the full inventory from the `v_inventory` view
  (the schema's views do the joins; the C++ never re-implements them). NULLs
  are preserved as JSON `null`, matching the Python backend's contract.
* **`GET /requests`** — one object per shipment line item, byte-compatible
  with the Render backend (verified: `jq -S`-normalized output of both
  services diffs empty against live data). Stored status enums are mapped
  back to display text (`under_review` → `"Under Review"`).
* **`GET /inventory/availability`** — per-item availability using the exact
  rule the Render backend applies (reverse-engineered and verified against
  all 1384 live rows): an item reserved for a shipment is `"Requested"`
  (tagged `Requested by <org>`); otherwise quantity ≤ 5 is `"Limited"`
  (tagged `Low Stock`); otherwise `"Available"`.

### Known divergences from the Render backend

* `/requests` emits `"Review Flag": "FALSE"` always — the flag isn't stored
  in the schema.
* `/inventory/availability` keys rows by `item_id`, not the backend's raw
  spreadsheet `sheet_row`, which local Postgres has no way to know.
* The backend treats a quantity cell that isn't a clean integer (e.g.
  `"271 (0.68 lbs)"`) as `Limited` regardless of size; we store the parsed
  number (271), so a junk-text cell parsing to > 5 shows `Available` here.
  Every other row classifies identically.
* `"Requested"` rows only appear once `inventory_item.reserved_for` is
  populated; the sync (like `monitor.py` before it) doesn't ingest the
  sheet's reservation column yet.

## The sync cycle (milestone 4, replaces `monitor.py`)

Every 300 seconds (and once ~1 s after startup):

1. **Fetch** `/api/supplies` and `/requests` from the Render backend
   asynchronously (90 s timeout, matching the old `curl -m 90`). Either feed
   failing is logged and skipped; the other still syncs.
2. **Hash** every supplies row as `md5(json.dumps(row, sort_keys=True))` —
   the same `source_hash` monitor.py wrote, reproduced *byte-for-byte* in
   C++ (`py_dump` in `sync.cpp` replicates Python's `", "`/`": "` separators
   and `ensure_ascii` `\uXXXX` escaping, including surrogate pairs). This
   matters: the database already holds thousands of these hashes, and any
   encoding drift would re-insert the entire feed as "new". Verified against
   the live DB: the first C++ cycle recognized all 1384 existing rows.
3. **Sync, in one transaction**: unseen supply rows are normalized
   (`normalize.cpp`) and inserted through the upsert chain
   manufacturer → supply → box → inventory_item (`ON CONFLICT` upserts, all
   named prepared statements in `db.cpp`); request rows upsert the requester
   and either create a shipment + line item or log a status change. The whole
   cycle commits or rolls back together.
4. **Check**: six consistency queries (negative stock, reservations against
   closed shipments, unflagged expired stock, duplicate supply names across
   manufacturers, zero-quantity items still boxed, flagged items awaiting
   review). Each runs in its own transaction so one broken check can't poison
   the others; findings are logged as warnings.

Log lines keep monitor.py's vocabulary (`NEW` / `CHANGE` / `SYNC` / `OK` /
warnings) via spdlog, so eyeballing the output feels the same. To retire
`monitor.py`, point the launchd plist at `build/b4p-service` instead.

## SQL policy

All SQL lives in `Db::prepare_all()` as named prepared statements — one place
to audit, and parameterization (`$1, $2, …`) is the only injection-safe path;
nothing ever string-concatenates values into a query. Reads lean on the
schema's views (`v_inventory`) rather than duplicating joins in C++.

## Roadmap status

* **M1 HTTP + Postgres** — done (`/health`, `/api/supplies`)
* **M2 dashboard compatibility** — done (`/requests` byte-identical live;
  `/inventory/availability` rule verified against all live rows)
* **M3 normalize + tests** — done (`normalize.cpp` + `ctest` suite; using a
  dependency-free assert harness — swap in Catch2 if it gets installed)
* **M4 sync loop** — done (async fetch on the event loop, one-transaction
  sync, checks; hash parity verified against the live DB)
* **M5 hardening** — open: connection pool inside `Db`, config file instead
  of the constants at the top of `main.cpp`, graceful shutdown, structured
  error responses.
