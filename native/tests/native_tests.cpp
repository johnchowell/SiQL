#include "siql_native.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <thread>
#include <stdexcept>
#include <string>
#include <vector>

static siql_value integer(int64_t n) { return {SIQL_INT, n, 0, nullptr, 0}; }
static siql_value boolean(bool n) { return {SIQL_BOOL, n, 0, nullptr, 0}; }
static siql_value real(double n) { return {SIQL_FLOAT, 0, n, nullptr, 0}; }
static siql_value string(const std::string &s) { return {SIQL_STRING, 0, 0, s.data(), s.size()}; }
static void ok(siql_table *table, int status) {
    if (status) throw std::runtime_error(siql_last_error(table));
}
static std::vector<size_t> find(siql_table *table, size_t column, siql_value value) {
    size_t length = siql_find(table, column, value, nullptr, 0);
    if (length == SIZE_MAX) throw std::runtime_error(siql_last_error(table));
    std::vector<size_t> out(length);
    if (siql_find(table, column, value, out.data(), out.size()) != length)
        throw std::runtime_error(siql_last_error(table));
    return out;
}
int main() {
    const auto file = std::filesystem::temp_directory_path() / "siql-native-core-test.siql";
    std::filesystem::remove(file);
    siql_table *table = siql_open(file.string().c_str());
    if (!table) throw std::runtime_error(siql_last_error(nullptr));
    ok(table, siql_add_column(table, "name", "str"));
    ok(table, siql_add_column(table, "number", "int"));
    const std::string a = "An'na";
    siql_value first[] = {string(a), integer(1)};
    ok(table, siql_append(table, first, 2));
    assert((find(table, 1, real(1.0)) == std::vector<size_t>{0}));
    assert((find(table, 1, boolean(true)) == std::vector<size_t>{0}));
    const std::string b = "\xf0\x9f\x98\x80";
    siql_value second[] = {string(b), integer(INT64_MAX)};
    ok(table, siql_append(table, second, 2));
    assert(find(table, 1, real(9007199254740992.0)).empty());
    assert((find(table, 1, integer(INT64_MAX)) == std::vector<size_t>{1}));
    siql_value changed;
    ok(table, siql_get(table, 0, 0, &changed));
    assert(std::string(changed.data, changed.length) == a);
    ok(table, siql_set(table, 0, 1, integer(2)));
    assert(find(table, 1, integer(1)).empty());
    assert((find(table, 1, integer(2)) == std::vector<size_t>{0}));
    siql_close(table);

    table = siql_open(file.string().c_str());
    if (!table) throw std::runtime_error(siql_last_error(nullptr));
    assert(siql_row_count(table) == 2 && siql_column_count(table) == 2);
    assert((find(table, 0, string(b)) == std::vector<size_t>{1}));
    std::vector<std::thread> readers;
    for (int worker = 0; worker < 4; ++worker)
        readers.emplace_back([table] {
            for (int i = 0; i < 1000; ++i)
                assert((find(table, 1, integer(INT64_MAX)) == std::vector<size_t>{1}));
        });
    for (auto &reader : readers) reader.join();
    ok(table, siql_delete(table, 0));
    assert((find(table, 1, integer(INT64_MAX)) == std::vector<size_t>{0}));
    siql_set_tree(table, false);
    assert((find(table, 1, integer(INT64_MAX)) == std::vector<size_t>{0}));
    siql_set_tree(table, true);
    assert((find(table, 1, integer(INT64_MAX)) == std::vector<size_t>{0}));
    siql_close(table);
    std::filesystem::remove(file);
    std::cout << "native core checks passed\n";
}
