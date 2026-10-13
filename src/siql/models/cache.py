"""A byte-budgeted LRU for live exact-search row references."""
import math
import sys
from collections import OrderedDict

DEFAULT_CACHE_BYTES = 100_000_000
_ENTRY_OVERHEAD = 512  # Conservative allowance for LRU links, dict/set slots, and entry metadata.


def cacheable(value):
    """Only immutable builtins with stable, side-effect-free equality may become keys."""
    kind = type(value)
    return kind in (str, int, bool, type(None)) or (kind is float and not math.isnan(value))


class QueryCache:
    def __init__(self, max_bytes=DEFAULT_CACHE_BYTES, *, traversed_nodes=False):
        self._entries = OrderedDict()
        self._columns = {}
        self._bytes = 0
        self._capacity_entries = 0
        self.hits = self.misses = self.evictions = self.invalidations = 0
        self.traversal_nodes_seen = self.traversal_admissions = 0
        self.traversed_nodes = traversed_nodes
        self._max_bytes = 0
        self.max_bytes = max_bytes

    @property
    def traversed_nodes(self):
        return self._traversed_nodes

    @traversed_nodes.setter
    def traversed_nodes(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("cache_traversed_nodes must be a boolean")
        self._traversed_nodes = enabled

    @property
    def max_bytes(self):
        return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, limit):
        if type(limit) is not int or limit < 0:
            raise ValueError("cache_max_bytes must be a nonnegative integer")
        self._max_bytes = limit
        self._trim()
        self._compact()

    def get(self, column, value):
        key = (column, value)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry[0]

    def put(self, column, value, rows):
        if not self._max_bytes:
            return False
        key = (column, value)
        if key in self._entries:
            self._entries.move_to_end(key)
            return False
        rows = tuple(rows)
        size = _ENTRY_OVERHEAD + sys.getsizeof(key) + sys.getsizeof(value) + sys.getsizeof(rows)
        if size > self._max_bytes:
            return False  # An oversized result must not flush the useful working set.
        while self._entries and self._bytes + size > self._max_bytes:
            self._evict()
        self._entries[key] = (rows, size)
        self._columns.setdefault(column, set()).add(key)
        self._bytes += size
        self._capacity_entries = max(self._capacity_entries, len(self._entries))
        return True

    def _remove(self, key):
        _, size = self._entries.pop(key)
        self._bytes -= size
        keys = self._columns[key[0]]
        keys.remove(key)
        if not keys:
            del self._columns[key[0]]
        if not self._entries:
            self._entries.clear()
            self._columns.clear()
            self._capacity_entries = 0
        elif len(self._entries) * 2 < self._capacity_entries:
            self._compact()

    def _compact(self):
        """Release mapping high-water allocations after shrinking or large evictions."""
        self._entries = OrderedDict(self._entries)
        self._columns = {}
        for key in self._entries:
            self._columns.setdefault(key[0], set()).add(key)
        self._capacity_entries = len(self._entries)

    def _evict(self):
        self._remove(next(iter(self._entries)))
        self.evictions += 1

    def _trim(self):
        while self._entries and self._bytes > self._max_bytes:
            self._evict()

    def invalidate(self, column):
        keys = self._columns.get(column)
        if keys:
            self.invalidations += len(keys)
            for key in tuple(keys):
                self._remove(key)

    def clear(self):
        self.invalidations += len(self._entries)
        self._entries.clear()
        self._columns.clear()
        self._bytes = 0
        self._capacity_entries = 0

    @property
    def info(self):
        return {"max_bytes": self._max_bytes, "bytes": self._bytes, "entries": len(self._entries),
                "hits": self.hits, "misses": self.misses, "evictions": self.evictions,
                "invalidations": self.invalidations, "cache_traversed_nodes": self._traversed_nodes,
                "traversal_nodes_seen": self.traversal_nodes_seen, "traversal_admissions": self.traversal_admissions}
