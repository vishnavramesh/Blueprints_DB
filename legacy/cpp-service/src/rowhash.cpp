#include "rowhash.h"

#include <cstdio>
#include <drogon/utils/Utilities.h>

using nlohmann::json;

namespace rowhash {

// ---------------------------------------------------------------------------
// Canonical row hashing.
//
// monitor.py identifies a feed row by md5(json.dumps(row, sort_keys=True)).
// The database already holds those hashes, so this port MUST produce the
// identical bytes or every historical row would look "new" and be re-inserted.
// Python's json.dumps defaults differ from nlohmann::json::dump() in two ways
// we have to reproduce:
//   * separators are ", " and ": " (nlohmann emits "," and ":")
//   * ensure_ascii=True — every char outside 0x20..0x7e becomes \uXXXX
//     (surrogate pairs above the BMP); nlohmann keeps raw UTF-8.
// Key order is fine for free: nlohmann objects iterate sorted (std::map) and
// the feed keys are ASCII, where byte order == codepoint order.
// ---------------------------------------------------------------------------

static void py_escape_string(const std::string& s, std::string& out) {
    out += '"';
    size_t i = 0, n = s.size();
    auto emit_u16 = [&](unsigned cp) {
        char buf[8];
        snprintf(buf, sizeof buf, "\\u%04x", cp);
        out += buf;
    };
    while (i < n) {
        unsigned char c = s[i];
        if (c == '"')       { out += "\\\""; i++; }
        else if (c == '\\') { out += "\\\\"; i++; }
        else if (c == '\n') { out += "\\n"; i++; }
        else if (c == '\r') { out += "\\r"; i++; }
        else if (c == '\t') { out += "\\t"; i++; }
        else if (c == '\b') { out += "\\b"; i++; }
        else if (c == '\f') { out += "\\f"; i++; }
        else if (c >= 0x20 && c <= 0x7e) { out += static_cast<char>(c); i++; }
        else if (c < 0x80) { emit_u16(c); i++; }  // control chars, DEL
        else {
            // decode one UTF-8 sequence to a codepoint
            unsigned cp = 0; int len = 0;
            if      ((c & 0xe0) == 0xc0) { cp = c & 0x1f; len = 2; }
            else if ((c & 0xf0) == 0xe0) { cp = c & 0x0f; len = 3; }
            else if ((c & 0xf8) == 0xf0) { cp = c & 0x07; len = 4; }
            else { emit_u16(0xfffd); i++; continue; }
            if (i + len > n) { emit_u16(0xfffd); i++; continue; }
            for (int k = 1; k < len; k++) cp = (cp << 6) | (s[i + k] & 0x3f);
            i += len;
            if (cp <= 0xffff) emit_u16(cp);
            else {  // surrogate pair, exactly like Python
                cp -= 0x10000;
                emit_u16(0xd800 + (cp >> 10));
                emit_u16(0xdc00 + (cp & 0x3ff));
            }
        }
    }
    out += '"';
}

static void py_dump(const json& v, std::string& out) {
    switch (v.type()) {
        case json::value_t::null:    out += "null"; break;
        case json::value_t::boolean: out += v.get<bool>() ? "true" : "false"; break;
        case json::value_t::string:  py_escape_string(v.get<std::string>(), out); break;
        case json::value_t::object: {
            out += '{';
            bool first = true;
            for (auto it = v.begin(); it != v.end(); ++it) {  // sorted (std::map)
                if (!first) out += ", ";
                first = false;
                py_escape_string(it.key(), out);
                out += ": ";
                py_dump(it.value(), out);
            }
            out += '}';
            break;
        }
        case json::value_t::array: {
            out += '[';
            bool first = true;
            for (const auto& e : v) {
                if (!first) out += ", ";
                first = false;
                py_dump(e, out);
            }
            out += ']';
            break;
        }
        default:  // integer / unsigned / float: nlohmann's shortest-roundtrip
            out += v.dump();  // matches repr() for ints; feed values are strings
    }
}

std::string py_dumps(const json& v) {
    std::string out;
    py_dump(v, out);
    return out;
}

std::string row_hash(const json& row) {
    std::string canon = py_dumps(row);
    std::string h = drogon::utils::getMd5(canon);
    for (auto& c : h) c = std::tolower(static_cast<unsigned char>(c));
    return h;
}

}  // namespace rowhash
