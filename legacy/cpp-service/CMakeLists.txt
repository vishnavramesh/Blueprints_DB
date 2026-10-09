cmake_minimum_required(VERSION 3.20)
project(b4p_service CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Warnings on from day one — much cheaper than retrofitting them later.
add_compile_options(-Wall -Wextra -Wpedantic)

# -DB4P_SANITIZE=thread   : ThreadSanitizer (checks the Db pool / sync threading)
# -DB4P_SANITIZE=address  : AddressSanitizer + UBSan
set(B4P_SANITIZE "" CACHE STRING "thread | address | empty")
if(B4P_SANITIZE STREQUAL "thread")
    add_compile_options(-fsanitize=thread -g -O1)
    add_link_options(-fsanitize=thread)
elseif(B4P_SANITIZE STREQUAL "address")
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -g -O1)
    add_link_options(-fsanitize=address,undefined)
endif()

# Homebrew installs everything under /opt/homebrew on Apple Silicon.
list(APPEND CMAKE_PREFIX_PATH /opt/homebrew)

find_package(Drogon CONFIG REQUIRED)      # HTTP framework + event loop
find_package(nlohmann_json CONFIG REQUIRED)
find_package(spdlog CONFIG REQUIRED)

# libpqxx ships a pkg-config file rather than a CMake config on brew.
find_package(PkgConfig REQUIRED)
pkg_check_modules(PQXX REQUIRED IMPORTED_TARGET libpqxx)

# Pure logic shared by the service and the tests.
add_library(b4p_core STATIC src/normalize.cpp src/rowhash.cpp)
target_include_directories(b4p_core PUBLIC src)
target_link_libraries(b4p_core PUBLIC Drogon::Drogon nlohmann_json::nlohmann_json)

add_executable(b4p-service
    src/main.cpp
    src/api.cpp
    src/db.cpp
    src/sync.cpp
)
target_compile_definitions(b4p-service PRIVATE
    B4P_DEFAULT_WEB_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/web")
target_link_libraries(b4p-service PRIVATE
    b4p_core
    spdlog::spdlog
    PkgConfig::PQXX
)

# ---------------------------------------------------------------------------
# Tests  (ctest --test-dir build;  ctest -L db  for the Postgres-backed ones)
# ---------------------------------------------------------------------------
enable_testing()

# Dependency-free assert harness (Catch2 isn't installed on this machine).
add_executable(normalize_test tests/normalize_test.cpp)
target_link_libraries(normalize_test PRIVATE b4p_core)
target_compile_definitions(normalize_test PRIVATE
    B4P_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures")
add_test(NAME unit COMMAND normalize_test)

# Randomized robustness: normalize/rowhash must never throw or crash.
add_executable(fuzz_test tests/fuzz_test.cpp)
target_link_libraries(fuzz_test PRIVATE b4p_core)
add_test(NAME fuzz COMMAND fuzz_test 200000)

# stdin JSON array -> one md5 per line; used by the Python hash-parity test.
add_executable(b4p-rowhash tests/rowhash_tool.cpp)
target_link_libraries(b4p-rowhash PRIVATE b4p_core)

find_package(Python3 COMPONENTS Interpreter)
if(Python3_FOUND)
    set(_env
        B4P_BIN=$<TARGET_FILE:b4p-service>
        B4P_ROWHASH_BIN=$<TARGET_FILE:b4p-rowhash>
        B4P_REPO=${CMAKE_CURRENT_SOURCE_DIR}/..)
    add_test(NAME golden COMMAND ${Python3_EXECUTABLE}
             ${CMAKE_CURRENT_SOURCE_DIR}/tests/golden_check.py)
    add_test(NAME hash_parity COMMAND ${Python3_EXECUTABLE}
             ${CMAKE_CURRENT_SOURCE_DIR}/tests/hash_parity_test.py)
    add_test(NAME db_constraints COMMAND ${Python3_EXECUTABLE} -m unittest -v constraints_test
             WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/tests)
    add_test(NAME sync_scenarios COMMAND ${Python3_EXECUTABLE} -m unittest -v scenario_test
             WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/tests)
    set_tests_properties(golden hash_parity db_constraints sync_scenarios
                         PROPERTIES ENVIRONMENT "${_env}")
    set_tests_properties(db_constraints sync_scenarios PROPERTIES LABELS db RUN_SERIAL TRUE)
endif()
