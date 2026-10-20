"""Thin ctypes adapter for the independent C++ core's current scalar CRUD API.

This module is intentionally separate from ``siql.Table`` until the native core
supports the existing API's complete value, index, SQL, and server behavior.
"""
import ctypes
import os
from pathlib import Path


class Value(ctypes.Structure):
    _fields_ = [('kind', ctypes.c_uint32), ('integer', ctypes.c_int64),
                ('real', ctypes.c_double), ('data', ctypes.c_void_p),
                ('length', ctypes.c_size_t)]


class CacheInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('max_bytes', 'bytes', 'entries', 'hits', 'misses', 'evictions', 'invalidations')]


def encode(value):
    kind = type(value)
    if value is None:
        return Value(0, 0, 0, None, 0)
    if kind is bool:
        return Value(1, int(value), 0, None, 0)
    if kind is int:
        if not -(1 << 63) <= value < (1 << 63):
            raise OverflowError('native scalar core currently accepts signed 64-bit integers')
        return Value(2, value, 0, None, 0)
    if kind is float:
        return Value(3, 0, value, None, 0)
    if kind is str or kind is bytes:
        data = value.encode('utf-8') if kind is str else value
        buffer = ctypes.create_string_buffer(data)
        result = Value(4 if kind is str else 5, 0, 0, ctypes.cast(buffer, ctypes.c_void_p), len(data))
        result._storage = buffer
        return result
    raise TypeError(f'native scalar core does not support {kind.__name__}')


def decode(value):
    if value.kind == 0:
        return None
    if value.kind == 1:
        return bool(value.integer)
    if value.kind == 2:
        return value.integer
    if value.kind == 3:
        return value.real
    data = ctypes.string_at(value.data, value.length)
    return data.decode('utf-8') if value.kind == 4 else data


def _library(path=None):
    if path is None:
        path = os.environ.get('SIQL_NATIVE_LIBRARY')
    if path is None:
        package_dir = Path(__file__).resolve().parent
        library = 'siql_native.dll' if os.name == 'nt' else (
            'libsiql_native.dylib' if os.uname().sysname == 'Darwin' else 'libsiql_native.so')
        bundled = package_dir / library
        root = package_dir.parents[1]
        path = bundled if bundled.exists() else root / 'build' / (
            'native-win/siql_native.dll' if os.name == 'nt' else library)
    lib = ctypes.CDLL(str(path))
    ptr = ctypes.c_void_p
    lib.siql_open.argtypes = [ctypes.c_char_p]
    lib.siql_open.restype = ptr
    lib.siql_close.argtypes = [ptr]
    lib.siql_last_error.argtypes = [ptr]
    lib.siql_last_error.restype = ctypes.c_char_p
    lib.siql_row_count.argtypes = [ptr]
    lib.siql_row_count.restype = ctypes.c_size_t
    lib.siql_column_count.argtypes = [ptr]
    lib.siql_column_count.restype = ctypes.c_size_t
    for name in ('siql_column_name', 'siql_column_type'):
        method = getattr(lib, name)
        method.argtypes = [ptr, ctypes.c_size_t]
        method.restype = ctypes.c_char_p
    lib.siql_add_column.argtypes = [ptr, ctypes.c_char_p, ctypes.c_char_p]
    lib.siql_add_column.restype = ctypes.c_int
    lib.siql_append.argtypes = [ptr, ctypes.POINTER(Value), ctypes.c_size_t]
    lib.siql_append.restype = ctypes.c_int
    lib.siql_append_batch.argtypes = [ptr, ctypes.POINTER(Value), ctypes.c_size_t, ctypes.c_size_t]
    lib.siql_append_batch.restype = ctypes.c_int
    lib.siql_set.argtypes = [ptr, ctypes.c_size_t, ctypes.c_size_t, Value]
    lib.siql_set.restype = ctypes.c_int
    lib.siql_delete.argtypes = [ptr, ctypes.c_size_t]
    lib.siql_delete.restype = ctypes.c_int
    lib.siql_get.argtypes = [ptr, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(Value)]
    lib.siql_get.restype = ctypes.c_int
    lib.siql_find.argtypes = [ptr, ctypes.c_size_t, Value, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
    lib.siql_find.restype = ctypes.c_size_t
    lib.siql_set_tree.argtypes = [ptr, ctypes.c_int]
    lib.siql_set_cache_max_bytes.argtypes = [ptr, ctypes.c_uint64]
    lib.siql_set_cache_max_bytes.restype = ctypes.c_int
    lib.siql_cache_snapshot.argtypes = [ptr, ctypes.POINTER(CacheInfo)]
    lib.siql_cache_snapshot.restype = ctypes.c_int
    lib.siql_clear_cache.argtypes = [ptr]
    return lib


class NativeTable:
    def __init__(self, file, *, library=None, cache_max_bytes=100_000_000):
        self.lib = _library(library)
        self.handle = self.lib.siql_open(os.fsencode(file))
        if not self.handle:
            raise OSError(self.lib.siql_last_error(None).decode())
        self._names = tuple(self.lib.siql_column_name(self.handle, i).decode()
                            for i in range(self.lib.siql_column_count(self.handle)))
        self.cache_max_bytes = cache_max_bytes

    def close(self):
        if self.handle:
            self.lib.siql_close(self.handle)
            self.handle = None

    def __del__(self):
        if getattr(self, 'handle', None):
            self.close()

    def _check(self, status):
        if status != 0:
            raise RuntimeError(self.lib.siql_last_error(self.handle).decode())

    @property
    def names(self):
        return list(self._names)

    def __len__(self):
        return self.lib.siql_row_count(self.handle)

    def add_column(self, name, type_name):
        self._check(self.lib.siql_add_column(self.handle, name.encode(), type_name.encode()))
        self._names += (name,)

    def append(self, **values):
        names = self._names
        if set(values) - set(names):
            raise KeyError(next(iter(set(values) - set(names))))
        encoded = [encode(values.get(name)) for name in names]
        payload = (Value * len(names))(*encoded)
        self._check(self.lib.siql_append(self.handle, payload, len(names)))

    def append_batch(self, rows):
        names = self._names
        payload = [encode(row.get(name)) for row in rows for name in names]
        encoded = (Value * len(payload))(*payload)
        self._check(self.lib.siql_append_batch(self.handle, encoded, len(rows), len(names)))

    def column(self, name):
        return self._names.index(name) if isinstance(name, str) else name

    def find(self, name, value):
        index = self.column(name)
        requested = encode(value)
        result = (ctypes.c_size_t * 16)()
        actual = self.lib.siql_find(self.handle, index, requested, result, len(result))
        if actual == ctypes.c_size_t(-1).value:
            self._check(-1)
        while actual > len(result):
            result = (ctypes.c_size_t * actual)()
            actual = self.lib.siql_find(self.handle, index, requested, result, len(result))
            if actual == ctypes.c_size_t(-1).value:
                self._check(-1)
        return list(result[:actual])

    def get(self, row, column):
        result = Value()
        self._check(self.lib.siql_get(self.handle, row, self.column(column), ctypes.byref(result)))
        return decode(result)

    def set(self, row, column, value):
        self._check(self.lib.siql_set(self.handle, row, self.column(column), encode(value)))

    def delete(self, row):
        self._check(self.lib.siql_delete(self.handle, row))

    def set_tree(self, enabled):
        self.lib.siql_set_tree(self.handle, bool(enabled))

    @property
    def cache_max_bytes(self):
        return self.cache_info.max_bytes

    @cache_max_bytes.setter
    def cache_max_bytes(self, limit):
        if type(limit) is not int or limit < 0 or limit >= 1 << 64:
            raise ValueError('cache_max_bytes must be a nonnegative 64-bit integer')
        self._check(self.lib.siql_set_cache_max_bytes(self.handle, limit))

    @property
    def cache_info(self):
        info = CacheInfo()
        self._check(self.lib.siql_cache_snapshot(self.handle, ctypes.byref(info)))
        return info

    def clear_cache(self):
        self.lib.siql_clear_cache(self.handle)
