// fuzz_test.cpp — randomized robustness test for everything that parses feed
// text. The feed is typed by volunteers and an AI, so the contract is simply:
// no input may throw or crash (std::stol overflow used to throw on a
// 30-digit box number). Mixes pure noise with mutations of real-looking values.
//
//   ./fuzz_test [iterations] [seed]
// Run under -DB4P_SANITIZE=address for memory-safety coverage.

#include <cstdio>
#include <cstdlib>
#include <random>
#include <string>
#include <vector>

#include "normalize.h"
#include "rowhash.h"

int main(int argc, char** argv) {
    long iters = argc > 1 ? std::atol(argv[1]) : 100000;
    unsigned seed = argc > 2 ? static_cast<unsigned>(std::atol(argv[2])) : std::random_device{}();
    std::mt19937 rng(seed);
    std::printf("fuzz: %ld iterations, seed %u\n", iters, seed);

    const std::vector<std::string> seeds = {
        "ER #1", "TC/RC 11", "8", "1 (full box)", "271 (0.68 lbs)", "3/15/27", "11/2026",
        "2028", "2026-05-23T05:23:06.638914+00:00", "Misc Non\xe2\x80\x90Medical",
        "Becton, Dickinson and Company", "Under Review", "99999999999999999999999999999",
        "IV-PROC #999999999999", "0", "-", "", "\xf0\x9f\x92\x89 syringe", "{scissors}",
    };
    const std::string alphabet = "0123456789/#- .:TZ+ERMISCtcrc\t\n\"\\{}";

    auto random_string = [&](size_t max_len) {
        std::string s;
        size_t n = rng() % (max_len + 1);
        for (size_t i = 0; i < n; i++) {
            switch (rng() % 3) {
                case 0: s += alphabet[rng() % alphabet.size()]; break;
                case 1: s += static_cast<char>(rng() % 256); break;  // arbitrary bytes
                default: s += static_cast<char>('0' + rng() % 10); break;
            }
        }
        return s;
    };
    auto mutate = [&](std::string s) {
        int edits = 1 + static_cast<int>(rng() % 4);
        for (int e = 0; e < edits; e++) {
            size_t pos = s.empty() ? 0 : rng() % (s.size() + 1);
            switch (rng() % 3) {
                case 0: s.insert(pos, random_string(8)); break;
                case 1: if (pos < s.size()) s.erase(pos, 1 + rng() % 4); break;
                default: if (pos < s.size()) s[pos] = static_cast<char>(rng() % 256); break;
            }
        }
        return s;
    };

    long failures = 0;
    for (long i = 0; i < iters; i++) {
        std::string s = (rng() % 2) ? random_string(40) : mutate(seeds[rng() % seeds.size()]);
        try {
            normalize::strip(s);
            normalize::norm_category(s);
            normalize::norm_mfr(s);
            normalize::parse_qty(s);
            normalize::parse_pallet(s);
            normalize::parse_date(s);
            normalize::parse_box(s);
            normalize::map_status(s);
            normalize::parse_timestamp(s);
            if (auto b = normalize::parse_box(s); b && (b->second < 1 || b->second > 2147483647L)) {
                std::printf("FAIL box number out of INTEGER range for %s\n", s.c_str());
                failures++;
            }
            if (long q = normalize::parse_qty(s); q < 0 || q > 999999) {
                std::printf("FAIL qty %ld out of range\n", q);
                failures++;
            }
            // Strings in rows are always valid UTF-8 after JSON parsing, but
            // the hasher must not crash on anything a json value can hold.
            nlohmann::json row = {{"Name", s}, {"Quantity", s}};
            rowhash::row_hash(row);
        } catch (const std::exception& e) {
            std::printf("FAIL threw on input (%zu bytes): %s\n", s.size(), e.what());
            if (++failures > 20) break;
        }
    }
    if (failures) {
        std::printf("%ld fuzz failure(s) (seed %u)\n", failures, seed);
        return 1;
    }
    std::printf("fuzz ok\n");
    return 0;
}
