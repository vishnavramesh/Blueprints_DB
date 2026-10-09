#include "normalize.h"

#include <algorithm>
#include <cctype>
#include <climits>
#include <regex>

namespace normalize {

const Rules& Rules::defaults() {
    static const Rules kDefaults = {
        /*categories=*/{
            {"iv supplies", 1}, {"misc surgical", 2}, {"wound care & bandages", 3},
            {"needles & syringes", 4}, {"catheters & briefs", 5},
            {"airway & oxygen", 6}, {"vitals & home health", 7}, {"gloves", 8},
            {"apparel (gowns, shoe covers, etc.)", 9}, {"masks & face shields", 10},
            {"sutures", 11}, {"misc non-medical", 12}, {"medical supplies", 13},
        },
        /*mfr_aliases=*/{
            {"bd", "BD"}, {"becton, dickinson and company", "BD"},
            {"becton dickinson", "BD"},
            {"cardinalhealth", "Cardinal Health"},
            {"cardinal health 200, llc", "Cardinal Health"},
            {"medline industries", "Medline"}, {"medline industries, lp", "Medline"},
            {"medline", "Medline"},
            {"owens & minor, inc.", "Owens & Minor"},
        },
        /*box_type_fix=*/{
            {"STN", "ST/N"}, {"TCRC", "TC/RC"}, {"TC/RC/", "TC/RC"}, {"TCRC/", "TC/RC"},
        },
        /*box_types=*/{
            "LEGACY", "TC/RC", "ST/N", "DME", "ER", "MISC",
            "PH/PPE", "IV-PROC", "PT-CARE", "WOUND",
        },
        /*statuses=*/{
            {"under review", "under_review"}, {"approved", "approved"},
            {"shipped", "shipped"}, {"delivered", "delivered"},
            {"cancelled", "cancelled"}, {"denied", "cancelled"},
        },
    };
    return kDefaults;
}

std::string strip(const std::string& s) {
    const char* ws = " \t\n\r\f\v";
    auto b = s.find_first_not_of(ws);
    if (b == std::string::npos) return "";
    auto e = s.find_last_not_of(ws);
    return s.substr(b, e - b + 1);
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    return s;
}

std::string upper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return std::toupper(c); });
    return s;
}

// ---------------------------------------------------------------------------

long norm_category(const std::string& raw, const Rules& rules) {
    std::string key = lower(strip(raw));
    // U+2010 HYPHEN (e2 80 90) and U+2011 NON-BREAKING HYPHEN (e2 80 91)
    for (const char* dash : {"\xe2\x80\x90", "\xe2\x80\x91"}) {
        for (size_t p; (p = key.find(dash)) != std::string::npos;)
            key.replace(p, 3, "-");
    }
    auto it = rules.categories.find(key);
    return it != rules.categories.end() ? it->second : 99;
}

std::optional<std::string> norm_mfr(const std::string& raw, const Rules& rules) {
    std::string name = strip(raw);
    std::string key = lower(name);
    if (name.empty() || key == "unspecified" || key == "unknown" || key == "n/a")
        return std::nullopt;
    auto it = rules.mfr_aliases.find(key);
    return it != rules.mfr_aliases.end() ? it->second : name;
}

// Digit string -> long without std::stol's out_of_range throw: anything with
// more than `max_digits` significant digits is reported as nullopt.
static std::optional<long> to_long(const std::string& digits, size_t max_digits) {
    auto nz = digits.find_first_not_of('0');
    if (nz == std::string::npos) return 0;
    if (digits.size() - nz > max_digits) return std::nullopt;
    return std::stol(digits.substr(nz));
}

static std::optional<std::string> first_digits(const std::string& raw) {
    static const std::regex re(R"(\d+)");
    std::smatch m;
    if (!std::regex_search(raw, m, re)) return std::nullopt;
    return m.str();
}

long parse_qty(const std::string& raw) {
    auto d = first_digits(raw);
    if (!d) return 0;
    auto n = to_long(*d, 18);
    return n ? std::min(*n, 999999L) : 999999L;  // huge -> capped, like Python
}

std::optional<long> parse_pallet(const std::string& raw) {
    auto d = first_digits(raw);
    if (!d) return std::nullopt;
    auto n = to_long(*d, 10);
    if (!n || *n > INT_MAX) return std::nullopt;
    return n;
}

// ---------------------------------------------------------------------------

static bool valid_date(long y, long m, long d) {
    if (y < 1 || m < 1 || m > 12 || d < 1) return false;
    static const int len[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    long max = len[m - 1];
    if (m == 2 && (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0))) max = 29;
    return d <= max;
}

static std::string iso(long y, long m, long d) {
    char buf[32];
    snprintf(buf, sizeof buf, "%04ld-%02ld-%02ld", y, m, d);
    return buf;
}

std::optional<std::string> parse_date(const std::string& raw) {
    std::string s = strip(raw);
    std::smatch m;
    static const std::regex ymd(R"(^(\d{4})-(\d{1,2})-(\d{1,2})$)");
    static const std::regex mdY(R"(^(\d{1,2})/(\d{1,2})/(\d{4})$)");
    static const std::regex mdy(R"(^(\d{1,2})/(\d{1,2})/(\d{2})$)");
    static const std::regex mY(R"(^(\d{1,2})/(\d{4})$)");
    static const std::regex Y(R"(^(\d{4})$)");
    if (std::regex_match(s, m, ymd)) {
        long y = std::stol(m[1]), mo = std::stol(m[2]), d = std::stol(m[3]);
        if (valid_date(y, mo, d)) return iso(y, mo, d);
    } else if (std::regex_match(s, m, mdY)) {
        long mo = std::stol(m[1]), d = std::stol(m[2]), y = std::stol(m[3]);
        if (valid_date(y, mo, d)) return iso(y, mo, d);
    } else if (std::regex_match(s, m, mdy)) {
        long mo = std::stol(m[1]), d = std::stol(m[2]), y2 = std::stol(m[3]);
        long y = y2 <= 68 ? 2000 + y2 : 1900 + y2;  // strptime pivot
        if (valid_date(y, mo, d)) return iso(y, mo, d);
    } else if (std::regex_match(s, m, mY)) {
        long mo = std::stol(m[1]), y = std::stol(m[2]);
        if (valid_date(y, mo, 1)) return iso(y, mo, 1);
    } else if (std::regex_match(s, m, Y)) {
        long y = std::stol(m[1]);
        if (valid_date(y, 1, 1)) return iso(y, 1, 1);
    }
    return std::nullopt;
}

// ---------------------------------------------------------------------------

std::optional<std::pair<std::string, long>> parse_box(const std::string& raw,
                                                      const Rules& rules) {
    std::string s = upper(strip(raw));
    static const std::regex bare(R"(^\d+$)");
    std::optional<long> n;
    if (std::regex_match(s, bare)) {
        n = to_long(s, 10);
        if (!n || *n < 1 || *n > INT_MAX) return std::nullopt;
        return {{"LEGACY", *n}};
    }
    static const std::regex re(R"(^([A-Z][A-Z/\-]*?)\s*#?\s*(\d+)$)");
    std::smatch m;
    if (!std::regex_match(s, m, re)) return std::nullopt;
    std::string btype = m[1];
    auto fix = rules.box_type_fix.find(btype);
    if (fix != rules.box_type_fix.end()) btype = fix->second;
    if (!rules.box_types.count(btype)) return std::nullopt;
    n = to_long(m[2], 10);
    if (!n || *n < 1 || *n > INT_MAX) return std::nullopt;  // box_number > 0 INTEGER
    return {{btype, *n}};
}

std::string map_status(const std::string& raw, const Rules& rules) {
    auto it = rules.statuses.find(lower(strip(raw)));
    return it != rules.statuses.end() ? it->second : "requested";
}

std::optional<std::string> parse_timestamp(const std::string& raw) {
    static const std::regex iso_ts(
        R"(^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}(:?\d{2})?)?)?$)");
    std::string s = strip(raw);
    if (!std::regex_match(s, iso_ts)) return std::nullopt;
    if (!parse_date(s.substr(0, 10))) return std::nullopt;  // 2026-02-31 etc.
    return s;
}

}  // namespace normalize
