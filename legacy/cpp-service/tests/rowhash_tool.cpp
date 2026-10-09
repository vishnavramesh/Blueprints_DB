// b4p-rowhash — reads a JSON array of feed rows on stdin, prints each row's
// source_hash (one per line). hash_parity_test.py compares this against
// Python's md5(json.dumps(row, sort_keys=True)) for the same rows.

#include <iostream>
#include <iterator>
#include <string>

#include "rowhash.h"

int main() {
    std::string in((std::istreambuf_iterator<char>(std::cin)), std::istreambuf_iterator<char>());
    auto rows = nlohmann::json::parse(in);
    for (const auto& row : rows) std::cout << rowhash::row_hash(row) << '\n';
    return 0;
}
