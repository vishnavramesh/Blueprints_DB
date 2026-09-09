#pragma once
// normalize.h — pure functions ported from load.py (MILESTONE 3).
// Every function mirrors its Python counterpart exactly, including the ugly
// edge cases; the Catch2 tests pin the real-world inputs from the feed.

#include <optional>
#include <string>
#include <utility>

namespace normalize {

// python str.strip() / .lower() / .upper(), ASCII-only (feed keys are ASCII;
// the one unicode wrinkle — the U+2010/U+2011 dashes — is handled explicitly
// in norm_category).
std::string strip(const std::string& s);
std::string lower(std::string s);
std::string upper(std::string s);

// "Wound Care & Bandages" -> 3; unknown -> 99 ("Unspecified").
// Handles the unicode hyphens U+2010/U+2011 seen in "Misc Non‐Medical".
long norm_category(const std::string& raw);

// "becton dickinson" -> "BD"; "unspecified"/"unknown"/"n/a"/"" -> nullopt.
std::optional<std::string> norm_mfr(const std::string& raw);

// First run of digits, capped at 999999; none -> 0.  "1 (full box)" -> 1.
long parse_qty(const std::string& raw);

// First run of digits or nullopt (no cap — mirrors load.parse_pallet).
std::optional<long> parse_pallet(const std::string& raw);

// Tries %Y-%m-%d, %m/%d/%Y, %m/%d/%y, %m/%Y, %Y in order (Python strptime
// semantics: two-digit years 00-68 -> 20xx, 69-99 -> 19xx; %m/%Y -> day 1;
// %Y -> Jan 1). Returns ISO "YYYY-MM-DD" for SQL, or nullopt.
std::optional<std::string> parse_date(const std::string& raw);

// "ER #1" / "MISC 2" / "TC/RC 11" -> {type, number}; bare "8" -> {"LEGACY", 8};
// typo'd types (STN, TCRC, ...) fixed; unknown types -> nullopt.
std::optional<std::pair<std::string, long>> parse_box(const std::string& raw);

// Dashboard status text -> shipment.status enum value; unknown -> "requested".
std::string map_status(const std::string& raw);

}  // namespace normalize
