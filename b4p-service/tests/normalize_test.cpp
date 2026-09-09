// normalize_test.cpp — MILESTONE 3 tests, pinning the nasty real-world inputs
// from the live feed. Each expectation is checked against load.py's behavior.
// Plain-assert harness (no Catch2 dependency): prints each failure and exits
// non-zero if any expectation failed.

#include "normalize.h"

#include <cstdio>
#include <string>

using namespace normalize;

static int failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                                 \
        }                                                               \
    } while (0)

int main() {
    // --- strip / lower / upper ---------------------------------------------
    CHECK(strip("  x \t\n") == "x");
    CHECK(strip("   ") == "");
    CHECK(lower("A/b C") == "a/b c");
    CHECK(upper("er #1") == "ER #1");

    // --- norm_category ------------------------------------------------------
    CHECK(norm_category("Wound Care & Bandages") == 3);
    CHECK(norm_category("  gloves ") == 8);
    CHECK(norm_category("Misc Non-Medical") == 12);
    CHECK(norm_category("Misc Non\xe2\x80\x90Medical") == 12);  // U+2010 hyphen
    CHECK(norm_category("Misc Non\xe2\x80\x91Medical") == 12);  // U+2011 nb-hyphen
    CHECK(norm_category("Apparel (Gowns, Shoe Covers, etc.)") == 9);
    CHECK(norm_category("no such thing") == 99);
    CHECK(norm_category("") == 99);

    // --- norm_mfr -----------------------------------------------------------
    CHECK(norm_mfr("becton dickinson") == std::optional<std::string>("BD"));
    CHECK(norm_mfr("Becton, Dickinson and Company") ==
          std::optional<std::string>("BD"));
    CHECK(norm_mfr("Medline Industries, LP") == std::optional<std::string>("Medline"));
    CHECK(norm_mfr("Unspecified") == std::nullopt);
    CHECK(norm_mfr("unknown") == std::nullopt);
    CHECK(norm_mfr("N/A") == std::nullopt);
    CHECK(norm_mfr("  ") == std::nullopt);
    CHECK(norm_mfr(" Smiths Medical ") == std::optional<std::string>("Smiths Medical"));

    // --- parse_qty / parse_pallet ------------------------------------------
    CHECK(parse_qty("1 (full box)") == 1);
    CHECK(parse_qty("20 (individual pens NOT boxes)") == 20);
    CHECK(parse_qty("271 (0.68 lbs)") == 271);
    CHECK(parse_qty("-") == 0);
    CHECK(parse_qty("") == 0);
    CHECK(parse_qty("4x5") == 4);
    CHECK(parse_qty("99999999999") == 999999);  // capped
    CHECK(parse_pallet("Pallet 7") == std::optional<long>(7));
    CHECK(parse_pallet("n/a") == std::nullopt);

    // --- parse_date ---------------------------------------------------------
    CHECK(parse_date("2027-03-15") == std::optional<std::string>("2027-03-15"));
    CHECK(parse_date("3/15/2027") == std::optional<std::string>("2027-03-15"));
    CHECK(parse_date("3/15/27") == std::optional<std::string>("2027-03-15"));
    CHECK(parse_date("3/15/99") == std::optional<std::string>("1999-03-15"));  // pivot
    CHECK(parse_date("3/15/68") == std::optional<std::string>("2068-03-15"));  // pivot
    CHECK(parse_date("11/2026") == std::optional<std::string>("2026-11-01"));
    CHECK(parse_date("2028") == std::optional<std::string>("2028-01-01"));
    CHECK(parse_date("2/29/2024") == std::optional<std::string>("2024-02-29"));  // leap
    CHECK(parse_date("2/29/2023") == std::nullopt);
    CHECK(parse_date("13/1/2024") == std::nullopt);
    CHECK(parse_date("soon") == std::nullopt);
    CHECK(parse_date("") == std::nullopt);

    // --- parse_box ----------------------------------------------------------
    using Box = std::optional<std::pair<std::string, long>>;
    CHECK(parse_box("ER #1") == Box({"ER", 1}));
    CHECK(parse_box("MISC 2") == Box({"MISC", 2}));
    CHECK(parse_box("TC/RC 11") == Box({"TC/RC", 11}));
    CHECK(parse_box("8") == Box({"LEGACY", 8}));            // bare number
    CHECK(parse_box("stn 3") == Box({"ST/N", 3}));          // typo fix
    CHECK(parse_box("TCRC 4") == Box({"TC/RC", 4}));        // typo fix
    CHECK(parse_box("iv-proc #12") == Box({"IV-PROC", 12}));
    CHECK(parse_box("BANANA 1") == std::nullopt);           // unknown type
    CHECK(parse_box("ER") == std::nullopt);                 // no number
    CHECK(parse_box("") == std::nullopt);

    // --- map_status ---------------------------------------------------------
    CHECK(map_status("Under Review") == "under_review");
    CHECK(map_status("DENIED") == "cancelled");
    CHECK(map_status("shipped ") == "shipped");
    CHECK(map_status("whatever") == "requested");
    CHECK(map_status("") == "requested");

    if (failures) {
        std::printf("%d expectation(s) FAILED\n", failures);
        return 1;
    }
    std::printf("all normalize tests passed\n");
    return 0;
}
