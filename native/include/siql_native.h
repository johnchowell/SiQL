#ifndef SIQL_NATIVE_H
#define SIQL_NATIVE_H
#include <stddef.h>
#include <stdint.h>
#ifdef _WIN32
# ifdef SIQL_NATIVE_BUILD
#  define SIQL_API __declspec(dllexport)
# else
#  define SIQL_API __declspec(dllimport)
# endif
#else
# define SIQL_API __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif

/* A standalone C ABI. No Python or .NET headers or runtime are used by this library.
   Pointers supplied by callers must remain valid for the duration of a call. */
typedef struct siql_table siql_table;
enum siql_kind {
    SIQL_NULL = 0, SIQL_BOOL = 1, SIQL_INT = 2, SIQL_FLOAT = 3,
    SIQL_STRING = 4, SIQL_BYTES = 5
};
typedef struct siql_value {
    uint32_t kind;
    int64_t integer;
    double real;
    const char *data;
    size_t length;
} siql_value;
typedef struct siql_cache_info {
    uint64_t max_bytes, bytes, entries, hits, misses, evictions, invalidations;
} siql_cache_info;

SIQL_API siql_table *siql_open(const char *path);
SIQL_API void siql_close(siql_table *table);
SIQL_API const char *siql_last_error(const siql_table *table);
SIQL_API size_t siql_row_count(const siql_table *table);
SIQL_API size_t siql_column_count(const siql_table *table);
SIQL_API const char *siql_column_name(const siql_table *table, size_t column);
SIQL_API const char *siql_column_type(const siql_table *table, size_t column);
SIQL_API int siql_add_column(siql_table *table, const char *name, const char *type);
SIQL_API int siql_append(siql_table *table, const siql_value *values, size_t count);
SIQL_API int siql_append_batch(siql_table *table, const siql_value *values, size_t row_count, size_t column_count);
SIQL_API int siql_set(siql_table *table, size_t row, size_t column, siql_value value);
SIQL_API int siql_delete(siql_table *table, size_t row);
SIQL_API int siql_get(const siql_table *table, size_t row, size_t column, siql_value *out);
/* Returns result length; use out=NULL to size the buffer. A caller-supplied buffer
   must have at least the returned length; smaller buffers are left untouched. */
SIQL_API size_t siql_find(siql_table *table, size_t column, siql_value value, size_t *out, size_t capacity);
SIQL_API void siql_set_tree(siql_table *table, int enabled);
SIQL_API int siql_set_cache_max_bytes(siql_table *table, uint64_t limit);
SIQL_API int siql_cache_snapshot(const siql_table *table, siql_cache_info *out);
SIQL_API void siql_clear_cache(siql_table *table);
SIQL_API const char *siql_core_version(void);

#ifdef __cplusplus
}
#endif
#endif
