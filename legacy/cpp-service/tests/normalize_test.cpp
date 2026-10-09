// normalize_test.cpp — MILESTONE 3 tests, pinning the nasty real-world inputs
// from the live feed. Each expectation is checked against load.py's behavior.
// Plain-assert harness (no Catch2 dependency): prints each failure and exits
// non-zero if any expectation failed.

#include "normalize.h"

#include <cstdio>
#include <fstream>
#include <string>

#include <nlohmann/json.hpp>

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


    // --- overflow: never throw, never exceed the INTEGER columns -------------
    CHECK(parse_box("123456789012345678901234567890") == std::nullopt);
    CHECK(parse_box("ER 99999999999") == std::nullopt);
    CHECK(parse_box("0") == std::nullopt);                  // box_number > 0
    CHECK(parse_box("ER 007") == Box({"ER", 7}));
    CHECK(parse_pallet("pallet 99999999999") == std::nullopt);
    CHECK(parse_qty("123456789012345678901234567890") == 999999);
    CHECK(parse_date("0000") == std::nullopt);

    // --- parse_timestamp ------------------------------------------------------
    CHECK(parse_timestamp("2026-05-23T05:23:06.638914+00:00") ==
          std::optional<std::string>("2026-05-23T05:23:06.638914+00:00"));
    CHECK(parse_timestamp("2026-05-23 05:23") == std::optional<std::string>("2026-05-23 05:23"));
    CHECK(parse_timestamp("2026-05-23") == std::optional<std::string>("2026-05-23"));
    CHECK(parse_timestamp("2026-02-31T00:00:00Z") == std::nullopt);
    CHECK(parse_timestamp("yesterday") == std::nullopt);
    CHECK(parse_timestamp("") == std::nullopt);

    // --- Rules loaded from the database override the built-ins ---------------
    {
        Rules r = Rules::defaults();
        r.categories["resp"] = 6;
        r.mfr_aliases["covidien lp"] = "Covidien";
        r.box_types.insert("OR");
        r.box_type_fix["O.R."] = "OR";
        r.statuses["on hold"] = "under_review";
        CHECK(norm_category("RESP", r) == 6);
        CHECK(norm_category("RESP") == 99);
        CHECK(norm_mfr("Covidien LP", r) == std::optional<std::string>("Covidien"));
        CHECK(parse_box("or 4", r) == Box({"OR", 4}));
        CHECK(parse_box("or 4") == std::nullopt);
        CHECK(map_status("On Hold", r) == "under_review");
    }

    // --- golden file: every distinct real feed value, expected per load.py ---
    // (regenerate with scripts/gen_golden.py; golden_check.py keeps it honest)
    {
        std::ifstream in(B4P_FIXTURES "/normalize_golden.json");
        if (!in) {
            std::printf("FAIL cannot open normalize_golden.json\n");
            failures++;
        } else {
            auto g = nlohmann::json::parse(in);
            auto opt_str = [](const nlohmann::json& v) {
                return v.is_null() ? std::optional<std::string>() : v.get<std::string>();
            };
            int n = 0;
            auto golden_fail = [&](const char* fn, const std::string& in) {
                std::printf("FAIL golden %s(\"%s\")\n", fn, in.c_str());
                failures++;
            };
            for (auto& [in, want] : g["norm_category"].items())
                if (n++, norm_category(in) != want.get<long>()) golden_fail("norm_category", in);
            for (auto& [in, want] : g["norm_mfr"].items())
                if (n++, norm_mfr(in) != opt_str(want)) golden_fail("norm_mfr", in);
            for (auto& [in, want] : g["parse_qty"].items())
                if (n++, parse_qty(in) != want.get<long>()) golden_fail("parse_qty", in);
            for (auto& [in, want] : g["parse_date"].items())
                if (n++, parse_date(in) != opt_str(want)) golden_fail("parse_date", in);
            for (auto& [in, want] : g["parse_pallet"].items()) {
                n++;
                auto got = parse_pallet(in);
                if (want.is_null() ? got.has_value() : got != want.get<long>())
                    golden_fail("parse_pallet", in);
            }
            for (auto& [in, want] : g["parse_box"].items()) {
                n++;
                auto got = parse_box(in);
                Box w = want.is_null() ? Box() : Box({want[0].get<std::string>(), want[1].get<long>()});
                if (got != w) golden_fail("parse_box", in);
            }
            for (auto& [in, want] : g["map_status"].items())
                if (n++, map_status(in) != want.get<std::string>()) golden_fail("map_status", in);
            std::printf("golden: %d real-world values checked\n", n);
        }
    }

    if (failures) {
        std::printf("%d expectation(s) FAILED\n", failures);
        return 1;
    }
    std::printf("all normalize tests passed\n");
    return 0;
}
