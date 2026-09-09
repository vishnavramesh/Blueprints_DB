#include "normalize.h"

#include <algorithm>
#include <cctype>
#include <map>
#include <regex>
#include <set>

namespace normalize {

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

long norm_category(const std::string& raw) {
    static const std::map<std::string, long> kCategoryIds = {
        {"iv supplies", 1}, {"misc surgical", 2}, {"wound care & bandages", 3},
        {"needles & syringes", 4}, {"catheters & briefs", 5},
        {"airway & oxygen", 6}, {"vitals & home health", 7}, {"gloves", 8},
        {"apparel (gowns, shoe covers, etc.)", 9}, {"masks & face shields", 10},
        {"sutures", 11}, {"misc non-medical", 12}, {"medical supplies", 13},
    };
    std::string key = lower(strip(raw));
    // U+2010 HYPHEN (e2 80 90) and U+2011 NON-BREAKING HYPHEN (e2 80 91)
    for (const char* dash : {"\xe2\x80\x90", "\xe2\x80\x91"}) {
        for (size_t p; (p = key.find(dash)) != std::string::npos;)
            key.replace(p, 3, "-");
    }
    auto it = kCategoryIds.find(key);
    return it != kCategoryIds.end() ? it->second : 99;
}

std::optional<std::string> norm_mfr(const std::string& raw) {
    static const std::map<std::string, std::string> kAliases = {
        {"bd", "BD"}, {"becton, dickinson and company", "BD"},
        {"becton dickinson", "BD"},
        {"cardinalhealth", "Cardinal Health"},
        {"cardinal health 200, llc", "Cardinal Health"},
        {"medline industries", "Medline"}, {"medline industries, lp", "Medline"},
        {"medline", "Medline"},
        {"owens & minor, inc.", "Owens & Minor"},
    };
    std::string name = strip(raw);
    std::string key = lower(name);
    if (name.empty() || key == "unspecified" || key == "unknown" || key == "n/a")
        return std::nullopt;
    auto it = kAliases.find(key);
    return it != kAliases.end() ? it->second : name;
}

static std::optional<long> first_digits(const std::string& raw) {
    static const std::regex re(R"(\d+)");
    std::smatch m;
    if (!std::regex_search(raw, m, re)) return std::nullopt;
    // Python int() has no overflow; cap defensively at 18 digits.
    std::string d = m.str().substr(0, 18);
    return std::stol(d);
}

long parse_qty(const std::string& raw) {
    auto n = first_digits(raw);
    return n ? std::min(*n, 999999L) : 0;
}

std::optional<long> parse_pallet(const std::string& raw) {
    return first_digits(raw);
}

// ---------------------------------------------------------------------------

static bool valid_date(long y, long m, long d) {
    if (m < 1 || m > 12 || d < 1) return false;
    static const int len[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    long max = len[m - 1];
    if (m == 2 && (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0))) max = 29;
    return d <= max;
}

static std::string iso(long y, long m, long d) {
    char buf[16];
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
        if (mo >= 1 && mo <= 12) return iso(y, mo, 1);
    } else if (std::regex_match(s, m, Y)) {
        return iso(std::stol(m[1]), 1, 1);
    }
    return std::nullopt;
}

// ---------------------------------------------------------------------------

std::optional<std::pair<std::string, long>> parse_box(const std::string& raw) {
    static const std::map<std::string, std::string> kTypeFix = {
        {"STN", "ST/N"}, {"TCRC", "TC/RC"}, {"TC/RC/", "TC/RC"}, {"TCRC/", "TC/RC"},
    };
    static const std::set<std::string> kValid = {
        "LEGACY", "TC/RC", "ST/N", "DME", "ER", "MISC",
        "PH/PPE", "IV-PROC", "PT-CARE", "WOUND",
    };
    std::string s = upper(strip(raw));
    static const std::regex bare(R"(^\d+$)");
    if (std::regex_match(s, bare)) return {{"LEGACY", std::stol(s)}};
    static const std::regex re(R"(^([A-Z][A-Z/\-]*?)\s*#?\s*(\d+)$)");
    std::smatch m;
    if (!std::regex_match(s, m, re)) return std::nullopt;
    std::string btype = m[1];
    auto fix = kTypeFix.find(btype);
    if (fix != kTypeFix.end()) btype = fix->second;
    if (!kValid.count(btype)) return std::nullopt;
    return {{btype, std::stol(m[2])}};
}

std::string map_status(const std::string& raw) {
    static const std::map<std::string, std::string> kStatusMap = {
        {"under review", "under_review"}, {"approved", "approved"},
        {"shipped", "shipped"}, {"delivered", "delivered"},
        {"cancelled", "cancelled"}, {"denied", "cancelled"},
    };
    auto it = kStatusMap.find(lower(strip(raw)));
    return it != kStatusMap.end() ? it->second : "requested";
}

}  // namespace normalize
