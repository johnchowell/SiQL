#include "siql_native.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <list>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {

struct Value {
    uint32_t kind = SIQL_NULL;
    int64_t integer = 0;
    double real = 0;
    std::string data;
};

Value copy_value(siql_value value) {
    if (value.kind > SIQL_BYTES) throw std::invalid_argument("unsupported value kind");
    if (value.kind == SIQL_FLOAT && !std::isfinite(value.real))
        throw std::invalid_argument("nonfinite floats cannot round-trip through a table file");
    if ((value.kind == SIQL_STRING || value.kind == SIQL_BYTES) && value.length && !value.data)
        throw std::invalid_argument("missing value bytes");
    Value result{value.kind, value.integer, value.real, {}};
    if (value.kind == SIQL_STRING || value.kind == SIQL_BYTES)
        result.data.assign(value.data ? value.data : "", value.length);
    if (value.kind == SIQL_STRING) {
        // Python table files are UTF-8. Reject invalid bytes at the ABI rather
        // than write a log Python cannot reopen.
        const auto &s = result.data;
        for (size_t i = 0; i < s.size();) {
            uint8_t c = uint8_t(s[i++]);
            if (c < 128) continue;
            unsigned n = c >= 0xc2 && c <= 0xdf ? 1 : c >= 0xe0 && c <= 0xef ? 2
                        : c >= 0xf0 && c <= 0xf4 ? 3 : 0;
            if (!n || i + n > s.size()) throw std::invalid_argument("invalid UTF-8 string");
            uint32_t cp = c & (n == 1 ? 31 : n == 2 ? 15 : 7);
            for (unsigned j = 0; j < n; ++j) {
                uint8_t next = uint8_t(s[i++]);
                if ((next & 0xc0) != 0x80) throw std::invalid_argument("invalid UTF-8 string");
                cp = (cp << 6) | (next & 63);
            }
            if (cp < (n == 1 ? 128u : n == 2 ? 2048u : 65536u) || cp > 0x10ffff
                || (cp >= 0xd800 && cp <= 0xdfff)) throw std::invalid_argument("invalid UTF-8 string");
        }
    }
    return result;
}

siql_value view(const Value &value) {
    return {value.kind, value.integer, value.real, value.data.data(), value.data.size()};
}

/* A float is represented by its exact binary rational, with powers of two
   removed from the significand. Thus 1, 1.0 and True have the same key,
   while 2^53+1 does not accidentally compare equal to the rounded double. */
struct Key {
    uint32_t kind = SIQL_NULL;
    std::string data;
    uint64_t magnitude = 0;
    int exponent = 0;
    bool negative = false;
    bool operator==(const Key &) const = default;
};

uint64_t mix(uint64_t value) {
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}
struct KeyHash {
    size_t operator()(const Key &key) const {
        uint64_t h = mix(key.kind) ^ mix(key.magnitude) ^ mix(static_cast<uint32_t>(key.exponent));
        h ^= mix(key.negative);
        for (unsigned char c : key.data) h = mix(h ^ c);
        return static_cast<size_t>(h);
    }
};

struct CacheKey {
    size_t column;
    Key key;
    bool operator==(const CacheKey &) const = default;
};
struct CacheKeyHash {
    size_t operator()(const CacheKey &value) const {
        return static_cast<size_t>(mix(KeyHash{}(value.key) ^ mix(value.column)));
    }
};
struct Row;
struct CacheEntry {
    CacheKey key;
    std::vector<Row *> rows;
    uint64_t bytes;
};

Key key_of(const Value &value) {
    Key result;
    if (value.kind == SIQL_STRING || value.kind == SIQL_BYTES) {
        result.kind = value.kind;
        result.data = value.data;
        return result;
    }
    if (value.kind == SIQL_NULL) return result;
    if (value.kind == SIQL_FLOAT && std::isnan(value.real)) {
        result.kind = 7; // A NaN is never a searchable equality key.
        return result;
    }
    if (value.kind == SIQL_FLOAT && std::isinf(value.real)) {
        result.kind = 8;
        result.negative = std::signbit(value.real);
        return result;
    }
    result.kind = 6;
    if (value.kind == SIQL_FLOAT) {
        const uint64_t bits = std::bit_cast<uint64_t>(value.real);
        const unsigned raw_exp = static_cast<unsigned>((bits >> 52) & 2047);
        result.magnitude = bits & UINT64_C(0xfffffffffffff);
        if (raw_exp) result.magnitude |= UINT64_C(1) << 52;
        result.exponent = (raw_exp ? static_cast<int>(raw_exp) - 1023 : -1022) - 52;
        result.negative = (bits >> 63) != 0;
    } else {
        const int64_t n = value.kind == SIQL_BOOL ? (value.integer != 0) : value.integer;
        result.negative = n < 0;
        result.magnitude = n < 0 ? uint64_t(-(n + 1)) + 1 : uint64_t(n);
    }
    if (result.magnitude == 0) {
        result.negative = false;
        result.exponent = 0;
    } else {
        const int trailing = std::countr_zero(result.magnitude);
        result.magnitude >>= trailing;
        result.exponent += trailing;
    }
    return result;
}

struct Row { uint64_t id; std::vector<Value> values; };
using Members = std::unordered_set<Row *>;
using Index = std::unordered_map<Key, Members, KeyHash>;

std::string quote(std::string_view text, bool bytes = false) {
    const char delimiter = text.find('\'') != text.npos && text.find('"') == text.npos ? '"' : '\'';
    std::string out = bytes ? "b" : "";
    out.push_back(delimiter);
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char c : text) {
        if (c == static_cast<unsigned char>(delimiter) || c == '\\') {
            out.push_back('\\'); out.push_back(static_cast<char>(c));
        } else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c < 32 || c == 127 || (bytes && c >= 128)) {
            out += "\\x"; out.push_back(hex[c >> 4]); out.push_back(hex[c & 15]);
        } else out.push_back(static_cast<char>(c));
    }
    out.push_back(delimiter);
    return out;
}

std::string literal(const Value &value) {
    switch (value.kind) {
        case SIQL_NULL: return "None";
        case SIQL_BOOL: return value.integer ? "True" : "False";
        case SIQL_INT: return std::to_string(value.integer);
        case SIQL_STRING: return quote(value.data);
        case SIQL_BYTES: return quote(value.data, true);
        case SIQL_FLOAT: {
            if (!std::isfinite(value.real)) throw std::invalid_argument("nonfinite values cannot be saved");
            std::ostringstream out;
            out << std::setprecision(std::numeric_limits<double>::max_digits10) << value.real;
            std::string result = out.str();
            if (result.find_first_of(".eE") == result.npos) result += ".0";
            return result;
        }
        default: throw std::invalid_argument("unsupported value kind");
    }
}

void append_utf8(std::string &out, uint32_t cp) {
    if (cp > 0x10ffff) throw std::invalid_argument("invalid Unicode escape");
    if (cp < 128) out.push_back(char(cp));
    else if (cp < 2048) { out.push_back(char(0xc0 | (cp >> 6))); out.push_back(char(0x80 | (cp & 63))); }
    else if (cp < 65536) {
        out.push_back(char(0xe0 | (cp >> 12)));
        out.push_back(char(0x80 | ((cp >> 6) & 63)));
        out.push_back(char(0x80 | (cp & 63)));
    } else {
        out.push_back(char(0xf0 | (cp >> 18)));
        out.push_back(char(0x80 | ((cp >> 12) & 63)));
        out.push_back(char(0x80 | ((cp >> 6) & 63)));
        out.push_back(char(0x80 | (cp & 63)));
    }
}

struct Literal {
    enum Tag { Atom, Tuple, List, Dict } tag = Atom;
    Value atom;
    std::vector<Literal> items;
    std::vector<std::pair<std::string, Literal>> entries;
};

class Reader {
    std::string_view text;
    size_t pos = 0;
    void space() { while (pos < text.size() && (text[pos] == ' ' || text[pos] == '\t' || text[pos] == '\r')) ++pos; }
    bool take(char ch) { space(); if (pos < text.size() && text[pos] == ch) { ++pos; return true; } return false; }
    void expect(char ch) { if (!take(ch)) throw std::invalid_argument("malformed table literal"); }
    static int digit(char c) {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        throw std::invalid_argument("malformed hexadecimal escape");
    }
    uint32_t hex_digits(int count) {
        uint32_t n = 0;
        for (int i = 0; i < count; ++i) {
            if (pos >= text.size()) throw std::invalid_argument("incomplete hexadecimal escape");
            n = (n << 4) | digit(text[pos++]);
        }
        return n;
    }
    Value string(bool bytes) {
        const char delimiter = text[pos++];
        std::string out;
        while (pos < text.size()) {
            const char c = text[pos++];
            if (c == delimiter) return {static_cast<uint32_t>(bytes ? SIQL_BYTES : SIQL_STRING), 0, 0, out};
            if (c != '\\') { out.push_back(c); continue; }
            if (pos >= text.size()) throw std::invalid_argument("incomplete string escape");
            const char escaped = text[pos++];
            switch (escaped) {
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'a': out.push_back('\a'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'v': out.push_back('\v'); break;
                case '\\': case '\'': case '"': out.push_back(escaped); break;
                case 'x': {
                    uint32_t cp = hex_digits(2);
                    if (bytes) out.push_back(char(cp));
                    else append_utf8(out, cp);
                    break;
                }
                case 'u': case 'U': {
                    if (bytes) throw std::invalid_argument("Unicode escape in bytes");
                    uint32_t cp = hex_digits(escaped == 'u' ? 4 : 8);
                    if (cp >= 0xd800 && cp <= 0xdfff)
                        throw std::invalid_argument("surrogate string requires a different log encoding");
                    append_utf8(out, cp);
                    break;
                }
                default:
                    if (escaped == 'N' || (escaped >= '0' && escaped <= '7'))
                        throw std::invalid_argument("unsupported string escape");
                    out.push_back('\\'); out.push_back(escaped); break;
            }
        }
        throw std::invalid_argument("unterminated string");
    }
public:
    explicit Reader(std::string_view input) : text(input) {}
    Literal value() {
        space();
        if (pos >= text.size()) throw std::invalid_argument("missing literal");
        if ((text[pos] == 'b' || text[pos] == 'B') && pos + 1 < text.size()
            && (text[pos + 1] == '\'' || text[pos + 1] == '"')) {
            ++pos; return {Literal::Atom, string(true), {}, {}};
        }
        if (text[pos] == '\'' || text[pos] == '"') return {Literal::Atom, string(false), {}, {}};
        if (text[pos] == '(' || text[pos] == '[') {
            const char open = text[pos++], close = open == '(' ? ')' : ']';
            Literal out; out.tag = open == '(' ? Literal::Tuple : Literal::List;
            if (take(close)) return out;
            do { out.items.push_back(value()); if (take(close)) return out; expect(','); } while (!take(close));
            return out;
        }
        if (take('{')) {
            Literal out; out.tag = Literal::Dict;
            if (take('}')) return out;
            do {
                Literal key = value();
                if (key.tag != Literal::Atom || key.atom.kind != SIQL_STRING)
                    throw std::invalid_argument("unsupported dictionary key");
                expect(':');
                out.entries.emplace_back(std::move(key.atom.data), value());
                if (take('}')) return out;
                expect(',');
            } while (!take('}'));
            return out;
        }
        const size_t start = pos;
        while (pos < text.size() && text[pos] != ',' && text[pos] != ')' && text[pos] != ']'
               && text[pos] != '}' && text[pos] != ' ' && text[pos] != '\t' && text[pos] != '\r') ++pos;
        const std::string token(text.substr(start, pos - start));
        if (token == "None") return {};
        if (token == "True" || token == "False") return {Literal::Atom, {SIQL_BOOL, token == "True", 0, {}}, {}, {}};
        size_t used = 0;
        try {
            if (token.find_first_of(".eE") != std::string::npos) {
                double number = std::stod(token, &used);
                if (used == token.size()) return {Literal::Atom, {SIQL_FLOAT, 0, number, {}}, {}, {}};
            } else {
                int64_t number = std::stoll(token, &used, 0);
                if (used == token.size()) return {Literal::Atom, {SIQL_INT, number, 0, {}}, {}, {}};
            }
        } catch (const std::exception &) { /* Report the same unsupported-literal error below. */ }
        throw std::invalid_argument("unsupported table literal: " + token);
    }
    Literal read() {
        Literal out = value();
        space();
        if (pos != text.size()) throw std::invalid_argument("trailing table literal");
        return out;
    }
};

std::string row_fields(const std::vector<std::string> &names, const std::vector<Value> &values) {
    std::string out = "{";
    for (size_t i = 0; i < names.size(); ++i) {
        if (i) out += ", ";
        out += quote(names[i]) + ": " + literal(values[i]);
    }
    return out + "}";
}

} // namespace

struct siql_table {
    mutable std::mutex mutex;
    std::string path;
    std::vector<std::string> names;
    std::vector<std::string> types;
    std::vector<std::unique_ptr<Row>> rows;
    std::vector<Index> indexes;
    std::list<CacheEntry> cache_lru;
    std::unordered_map<CacheKey, std::list<CacheEntry>::iterator, CacheKeyHash> cache_map;
    std::unordered_map<size_t, std::unordered_set<Key, KeyHash>> cache_columns;
    uint64_t cache_max_bytes = 100000000, cache_bytes = 0;
    uint64_t cache_hits = 0, cache_misses = 0, cache_evictions = 0, cache_invalidations = 0;
    std::unordered_map<Row *, size_t> positions;
    std::vector<size_t> deleted;
    bool positions_ready = false;
    bool tree = true;
    uint64_t next_id = 1;

    explicit siql_table(std::string file) : path(std::move(file)) {}
    std::filesystem::path file_path() const {
        return std::filesystem::path(std::u8string(path.begin(), path.end()));
    }
    size_t column(const std::string &name) const {
        auto found = std::find(names.begin(), names.end(), name);
        if (found == names.end()) throw std::out_of_range("unknown column: " + name);
        return size_t(found - names.begin());
    }
    void check_row(size_t row) const {
        if (row >= rows.size()) throw std::out_of_range("row index out of range");
    }
    void check_column(size_t col) const {
        if (col >= names.size()) throw std::out_of_range("column index out of range");
    }
    void file_append(const std::string &line) {
        std::ofstream out(file_path(), std::ios::binary | std::ios::app);
        if (!out || !(out << line << '\n')) throw std::runtime_error("could not append table change");
    }
    void clear_cache() {
        cache_invalidations += cache_map.size();
        cache_lru.clear(); cache_map.clear(); cache_columns.clear(); cache_bytes = 0;
    }
    void invalidate_cache(size_t column) {
        auto entries = cache_columns.find(column);
        if (entries == cache_columns.end()) return;
        for (const Key &key : entries->second) {
            auto found = cache_map.find({column, key});
            if (found == cache_map.end()) continue;
            cache_bytes -= found->second->bytes;
            cache_lru.erase(found->second);
            cache_map.erase(found);
            ++cache_invalidations;
        }
        cache_columns.erase(entries);
    }
    void evict_cache() {
        auto last = std::prev(cache_lru.end());
        cache_bytes -= last->bytes;
        CacheKey key = last->key;
        cache_map.erase(key);
        auto group = cache_columns.find(key.column);
        group->second.erase(key.key);
        if (group->second.empty()) cache_columns.erase(group);
        cache_lru.pop_back();
        ++cache_evictions;
    }
    std::optional<std::vector<Row *>> cached(size_t column, const Key &key) {
        if (!cache_max_bytes || key.kind == 7) return std::nullopt;
        auto found = cache_map.find({column, key});
        if (found == cache_map.end()) { ++cache_misses; return std::nullopt; }
        ++cache_hits;
        cache_lru.splice(cache_lru.begin(), cache_lru, found->second);
        return found->second->rows;
    }
    void remember(size_t column, const Key &key, const std::vector<Row *> &result) {
        if (!cache_max_bytes || key.kind == 7) return;
        uint64_t bytes = 512 + sizeof(CacheEntry) + key.data.size()
                         + result.size() * sizeof(Row *);
        if (bytes > cache_max_bytes || cache_map.contains({column, key})) return;
        while (!cache_lru.empty() && cache_bytes + bytes > cache_max_bytes) evict_cache();
        cache_lru.push_front({{column, key}, result, bytes});
        try {
            cache_map.emplace(cache_lru.front().key, cache_lru.begin());
            cache_columns[column].insert(key);
            cache_bytes += bytes;
        } catch (...) {
            cache_map.erase(cache_lru.front().key);
            cache_lru.pop_front();
            throw;
        }
    }
    void build_indexes() {
        indexes.clear(); indexes.resize(names.size());
        if (!tree) return;
        for (const auto &row : rows)
            for (size_t c = 0; c < names.size(); ++c)
                if (row->values[c].kind != SIQL_FLOAT || !std::isnan(row->values[c].real))
                    indexes[c][key_of(row->values[c])].insert(row.get());
    }
    void insert_index(Row *row, size_t col) {
        if (tree && (row->values[col].kind != SIQL_FLOAT || !std::isnan(row->values[col].real)))
            indexes[col][key_of(row->values[col])].insert(row);
    }
    void remove_index(Row *row, size_t col) {
        if (!tree) return;
        const Key key = key_of(row->values[col]);
        auto found = indexes[col].find(key);
        if (found != indexes[col].end()) {
            found->second.erase(row);
            if (found->second.empty()) indexes[col].erase(found);
        }
    }
    void add_column(std::string name, std::string type, size_t at) {
        if (std::find(names.begin(), names.end(), name) != names.end())
            throw std::invalid_argument("duplicate column: " + name);
        if (at > names.size()) at = names.size();
        names.insert(names.begin() + at, std::move(name));
        types.insert(types.begin() + at, std::move(type));
        for (const auto &row : rows) row->values.insert(row->values.begin() + at, {});
        clear_cache();
        build_indexes();
    }
    void insert_row(size_t at, std::vector<Value> values) {
        if (values.size() != names.size()) throw std::invalid_argument("wrong number of cells");
        if (at > rows.size()) at = rows.size();
        auto row = std::make_unique<Row>(Row{next_id++, std::move(values)});
        clear_cache();
        Row *ptr = row.get();
        rows.insert(rows.begin() + at, std::move(row));
        if (positions_ready && at == rows.size() - 1) positions[ptr] = at + deleted.size();
        else { positions_ready = false; positions.clear(); deleted.clear(); }
        for (size_t col = 0; col < names.size(); ++col) insert_index(ptr, col);
    }
    void set_cell(size_t row, size_t col, Value value) {
        check_row(row); check_column(col);
        invalidate_cache(col);
        Row *ptr = rows[row].get();
        remove_index(ptr, col);
        ptr->values[col] = std::move(value);
        insert_index(ptr, col);
    }
    void drop_row(size_t at) {
        check_row(at);
        clear_cache();
        Row *ptr = rows[at].get();
        if (positions_ready) {
            if (deleted.size() < (rows.size() - 1) / 4 + 64) {
                size_t old = positions.at(ptr);
                positions.erase(ptr);
                deleted.insert(std::lower_bound(deleted.begin(), deleted.end(), old), old);
            } else { positions_ready = false; positions.clear(); deleted.clear(); }
        }
        for (size_t col = 0; col < names.size(); ++col) remove_index(ptr, col);
        rows.erase(rows.begin() + at);
    }
    std::vector<size_t> row_positions(const std::vector<Row *> &found) {
        if (found.empty()) return {};
        if (!positions_ready) {
            positions.clear();
            for (size_t i = 0; i < rows.size(); ++i) positions[rows[i].get()] = i;
            deleted.clear(); positions_ready = true;
        }
        std::vector<size_t> result;
        result.reserve(found.size());
        for (Row *row : found) {
            const size_t old = positions.at(row);
            result.push_back(old - size_t(std::lower_bound(deleted.begin(), deleted.end(), old) - deleted.begin()));
        }
        std::sort(result.begin(), result.end());
        return result;
    }
    std::vector<size_t> find(size_t col, const Value &value) {
        check_column(col);
        const Key target = key_of(value);
        if (target.kind == 7) return {};
        if (!tree) {
            std::vector<size_t> result;
            for (size_t i = 0; i < rows.size(); ++i)
                if (key_of(rows[i]->values[col]) == target) result.push_back(i);
            return result;
        }
        if (auto hit = cached(col, target)) return row_positions(*hit);
        auto found = indexes[col].find(target);
        std::vector<Row *> matches;
        if (found != indexes[col].end()) matches.assign(found->second.begin(), found->second.end());
        remember(col, target, matches);
        return row_positions(matches);
    }
};

namespace {
thread_local std::string open_error;

int64_t number(const Literal &item) {
    if (item.tag != Literal::Atom || item.atom.kind != SIQL_INT)
        throw std::invalid_argument("expected integer position");
    return item.atom.integer;
}
const std::string &text(const Literal &item) {
    if (item.tag != Literal::Atom || item.atom.kind != SIQL_STRING)
        throw std::invalid_argument("expected text literal");
    return item.atom.data;
}
Value cell(const Literal &item) {
    if (item.tag != Literal::Atom) throw std::invalid_argument("native core does not yet support container cells");
    return copy_value(view(item.atom));
}
size_t position(int64_t value, size_t count, bool insert) {
    if (value < 0) {
        uint64_t magnitude = uint64_t(-(value + 1)) + 1;
        return magnitude > count ? 0 : count - size_t(magnitude);
    }
    if (insert) return std::min(uint64_t(value), uint64_t(count));
    if (uint64_t(value) >= count) throw std::out_of_range("row index out of range");
    return size_t(value);
}
void apply_op(siql_table &table, const Literal &op) {
    if (op.tag != Literal::Tuple || op.items.empty()) throw std::invalid_argument("expected diff tuple");
    const auto &a = op.items;
    const auto &code = text(a[0]);
    if (code == "+c" && a.size() == 4)
        table.add_column(text(a[1]), text(a[2]), position(number(a[3]), table.names.size(), true));
    else if (code == "-c" && a.size() == 2) {
        size_t col = table.column(text(a[1]));
        table.clear_cache();
        table.names.erase(table.names.begin() + col);
        table.types.erase(table.types.begin() + col);
        for (auto &row : table.rows) row->values.erase(row->values.begin() + col);
        table.build_indexes();
    } else if (code == "=c" && a.size() == 3)
        table.types[table.column(text(a[1]))] = text(a[2]);
    else if (code == ">c" && a.size() == 3) {
        if (std::find(table.names.begin(), table.names.end(), text(a[2])) != table.names.end())
            throw std::invalid_argument("duplicate column");
        table.names[table.column(text(a[1]))] = text(a[2]);
    } else if (code == "+r" && a.size() == 3) {
        if (a[2].tag != Literal::Dict) throw std::invalid_argument("expected row dictionary");
        std::vector<Value> values(table.names.size());
        for (const auto &[name, item] : a[2].entries) values[table.column(name)] = cell(item);
        table.insert_row(position(number(a[1]), table.rows.size(), true), std::move(values));
    } else if (code == "-r" && a.size() == 2)
        table.drop_row(position(number(a[1]), table.rows.size(), false));
    else if (code == "~" && a.size() == 4)
        table.set_cell(position(number(a[1]), table.rows.size(), false), table.column(text(a[2])), cell(a[3]));
    else throw std::invalid_argument("unknown or malformed diff operation");
}
void load(siql_table &table) {
    namespace fs = std::filesystem;
    const auto path = table.file_path();
    if (!fs::exists(path)) {
        std::ofstream create(path, std::ios::binary);
        if (!create) throw std::runtime_error("cannot create table file");
        return;
    }
    std::ifstream in(path, std::ios::binary);
    if (!in) throw std::runtime_error("cannot read table file");
    std::string data((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    const auto last = data.rfind('\n');
    const size_t end = last == std::string::npos ? 0 : last + 1;
    if (end < data.size()) fs::resize_file(path, end);
    size_t start = 0;
    size_t line = 0;
    while (start < end) {
        const size_t next = data.find('\n', start);
        ++line;
        std::string_view source(data.data() + start, next - start);
        try {
            if (source.find_first_not_of(" \t\r") != source.npos) {
                Literal ops = Reader(source).read();
                if (ops.tag == Literal::List) {
                    for (const auto &op : ops.items) apply_op(table, op);
                } else apply_op(table, ops);
            }
        } catch (const std::exception &e) {
            throw std::runtime_error("table line " + std::to_string(line) + ": " + e.what());
        }
        start = next + 1;
    }
}
template<class F> int guard(siql_table *table, F &&operation) {
    if (!table) { open_error = "null table handle"; return -1; }
    try {
        std::lock_guard lock(table->mutex);
        operation(); open_error.clear(); return 0;
    }
    catch (const std::exception &e) { open_error = e.what(); return -1; }
    catch (...) { open_error = "unknown native error"; return -1; }
}
}

extern "C" {
siql_table *siql_open(const char *path) {
    try {
        if (!path || !*path) throw std::invalid_argument("missing table path");
        auto table = std::make_unique<siql_table>(path);
        load(*table);
        open_error.clear();
        return table.release();
    } catch (const std::exception &e) { open_error = e.what(); return nullptr; }
}
void siql_close(siql_table *table) { delete table; }
const char *siql_last_error(const siql_table *) { return open_error.c_str(); }
size_t siql_row_count(const siql_table *table) {
    if (!table) return 0;
    std::lock_guard lock(table->mutex);
    return table->rows.size();
}
size_t siql_column_count(const siql_table *table) {
    if (!table) return 0;
    std::lock_guard lock(table->mutex);
    return table->names.size();
}
const char *siql_column_name(const siql_table *table, size_t column) {
    static thread_local std::string result;
    if (!table) return nullptr;
    std::lock_guard lock(table->mutex);
    if (column >= table->names.size()) return nullptr;
    result = table->names[column];
    return result.c_str();
}
const char *siql_column_type(const siql_table *table, size_t column) {
    static thread_local std::string result;
    if (!table) return nullptr;
    std::lock_guard lock(table->mutex);
    if (column >= table->types.size()) return nullptr;
    result = table->types[column];
    return result.c_str();
}
int siql_add_column(siql_table *table, const char *name, const char *type) {
    return guard(table, [&] {
        if (!name || !type) throw std::invalid_argument("missing column metadata");
        const std::string line = "('+c', " + quote(name) + ", " + quote(type) + ", "
            + std::to_string(table->names.size()) + ")";
        table->add_column(name, type, table->names.size());
        table->file_append(line);
    });
}
int siql_append(siql_table *table, const siql_value *values, size_t count) {
    return guard(table, [&] {
        if (count != table->names.size() || (count && !values))
            throw std::invalid_argument("wrong number of cells");
        std::vector<Value> row;
        row.reserve(count);
        for (size_t i = 0; i < count; ++i) row.push_back(copy_value(values[i]));
        const std::string line = "('+r', " + std::to_string(table->rows.size()) + ", "
            + row_fields(table->names, row) + ")";
        table->insert_row(table->rows.size(), std::move(row));
        table->file_append(line);
    });
}
int siql_append_batch(siql_table *table, const siql_value *values, size_t row_count, size_t column_count) {
    return guard(table, [&] {
        if (column_count != table->names.size() || (row_count && column_count && !values)
            || (column_count && row_count > SIZE_MAX / column_count))
            throw std::invalid_argument("wrong number of batch cells");
        if (!row_count) return;
        std::vector<std::vector<Value>> pending;
        pending.reserve(row_count);
        std::string line = "[";
        for (size_t row = 0; row < row_count; ++row) {
            std::vector<Value> cells;
            for (size_t col = 0; col < column_count; ++col)
                cells.push_back(copy_value(values[row * column_count + col]));
            if (row) line += ", ";
            line += "('+r', " + std::to_string(table->rows.size() + row) + ", "
                + row_fields(table->names, cells) + ")";
            pending.push_back(std::move(cells));
        }
        line += "]";
        for (auto &row : pending) table->insert_row(table->rows.size(), std::move(row));
        table->file_append(line);
    });
}
int siql_set(siql_table *table, size_t row, size_t column, siql_value value) {
    return guard(table, [&] {
        table->check_row(row); table->check_column(column);
        Value next = copy_value(value);
        const std::string line = "('~', " + std::to_string(row) + ", "
            + quote(table->names[column]) + ", " + literal(next) + ")";
        table->set_cell(row, column, std::move(next));
        table->file_append(line);
    });
}
int siql_delete(siql_table *table, size_t row) {
    return guard(table, [&] {
        table->check_row(row);
        table->drop_row(row);
        table->file_append("('-r', " + std::to_string(row) + ")");
    });
}
int siql_get(const siql_table *table, size_t row, size_t column, siql_value *out) {
    return guard(const_cast<siql_table *>(table), [&] {
        if (!out) throw std::invalid_argument("missing output buffer");
        table->check_row(row); table->check_column(column);
        *out = view(table->rows[row]->values[column]);
    });
}
size_t siql_find(siql_table *table, size_t column, siql_value value, size_t *out, size_t capacity) {
    if (!table) { open_error = "null table handle"; return SIZE_MAX; }
    try {
        std::lock_guard lock(table->mutex);
        auto result = table->find(column, copy_value(value));
        if (out && capacity >= result.size()) std::copy(result.begin(), result.end(), out);
        open_error.clear();
        return result.size();
    } catch (const std::exception &e) { open_error = e.what(); return SIZE_MAX; }
}
void siql_set_tree(siql_table *table, int enabled) {
    guard(table, [&] {
        bool wanted = enabled != 0;
        if (table->tree == wanted) return;
        table->tree = wanted;
        table->clear_cache();
        table->build_indexes();
        table->positions_ready = false;
        table->positions.clear(); table->deleted.clear();
    });
}
int siql_set_cache_max_bytes(siql_table *table, uint64_t limit) {
    return guard(table, [&] {
        table->cache_max_bytes = limit;
        while (!table->cache_lru.empty() && table->cache_bytes > limit) table->evict_cache();
    });
}
int siql_cache_snapshot(const siql_table *table, siql_cache_info *out) {
    return guard(const_cast<siql_table *>(table), [&] {
        if (!out) throw std::invalid_argument("missing cache snapshot output");
        *out = {table->cache_max_bytes, table->cache_bytes, table->cache_map.size(),
                table->cache_hits, table->cache_misses, table->cache_evictions,
                table->cache_invalidations};
    });
}
void siql_clear_cache(siql_table *table) { guard(table, [&] { table->clear_cache(); }); }
const char *siql_core_version(void) { return "0.1.0"; }
}
