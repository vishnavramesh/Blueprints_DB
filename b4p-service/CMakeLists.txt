cmake_minimum_required(VERSION 3.20)
project(b4p_service CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Warnings on from day one — much cheaper than retrofitting them later.
add_compile_options(-Wall -Wextra -Wpedantic)

# Homebrew installs everything under /opt/homebrew on Apple Silicon.
list(APPEND CMAKE_PREFIX_PATH /opt/homebrew)

find_package(Drogon CONFIG REQUIRED)      # HTTP framework + event loop
find_package(nlohmann_json CONFIG REQUIRED)
find_package(spdlog CONFIG REQUIRED)

# libpqxx ships a pkg-config file rather than a CMake config on brew.
find_package(PkgConfig REQUIRED)
pkg_check_modules(PQXX REQUIRED IMPORTED_TARGET libpqxx)

add_executable(b4p-service
    src/main.cpp
    src/db.cpp
    src/normalize.cpp   # MILESTONE 3: load.py's pure normalization rules
    src/sync.cpp        # MILESTONE 4: monitor.py's sync loop + checks
)

target_link_libraries(b4p-service PRIVATE
    Drogon::Drogon
    nlohmann_json::nlohmann_json
    spdlog::spdlog
    PkgConfig::PQXX
)

# MILESTONE 3: normalize tests. Dependency-free assert harness (Catch2 isn't
# installed on this machine; swap in Catch2::Catch2WithMain if it ever is).
enable_testing()
add_executable(normalize_test tests/normalize_test.cpp src/normalize.cpp)
target_include_directories(normalize_test PRIVATE src)
add_test(NAME unit COMMAND normalize_test)
