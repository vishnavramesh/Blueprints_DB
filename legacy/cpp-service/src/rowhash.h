#pragma once
// rowhash.h — the feed-row identity hash shared with monitor.py/load.py.
//
// md5(json.dumps(row, sort_keys=True)) reproduced byte-for-byte. Since
// migration 001 this hash is a *change detector* (the row's identity is its
// source_key), but it still has to match Python exactly: every hash already in
// the database was written by the Python loader, and a drift would make every
// row look edited.

#include <string>
#include <nlohmann/json.hpp>

namespace rowhash {

// Python's json.dumps(v, sort_keys=True) with default separators and
// ensure_ascii=True.
std::string py_dumps(const nlohmann::json& v);

// lowercase hex md5 of py_dumps(row)
std::string row_hash(const nlohmann::json& row);

}  // namespace rowhash
