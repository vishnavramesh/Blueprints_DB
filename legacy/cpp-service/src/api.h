#pragma once
// api.h — every HTTP route. main.cpp only wires config, Db, sync and this.
//
// Response conventions:
//   * success: JSON body (arrays for collections, objects otherwise)
//   * failure: {"error": {"code": "...", "message": "..."}} with a matching
//     status — 400 bad_request, 401 unauthorized, 404 not_found,
//     409 conflict (a database rule refused the change), 500 internal.
//   * the dashboard-compatible routes (/api/supplies with no query string,
//     /requests, /inventory/availability) keep the Render backend's exact
//     shape; see README "Known divergences".

#include <memory>
#include "config.h"

class Db;

namespace api {

void register_routes(std::shared_ptr<Db> db, const Config& cfg);

}  // namespace api
