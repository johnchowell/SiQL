"""Search primitives used by the tree and the interpreter. Each has a C version in `siql._speedups`, used when it was
compiled; set SIQL_PURE_PYTHON=1 to use these Python versions instead."""
import operator
import os
import random

EQ, NE, LT, LE, GT, GE, IS_NULL, NOT_NULL = range(8)
SCAN_OPS = {"=": EQ, "==": EQ, "!=": NE, "<>": NE, "<": LT, "<=": LE, ">": GT, ">=": GE}
_COMPARE = [operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge]


class _Node():
    __slots__ = ("key", "ids", "next")

    def __init__(self, key, height: int):
        self.key = key
        self.ids: set[int] = set()  # every cell holding this value
        self.next: list["_Node | None"] = [None] * height


class PySkipList():
    """Sorted keys with randomly placed express lanes.

    Each new key gets a random height and joins that many lanes, each lane holding about a quarter of the keys of
    the one below. A search runs along the sparsest lane while the next key is smaller than the target, then drops
    a lane, so it jumps over most keys instead of checking each one: about log(n) comparisons.
    """
    MAX_HEIGHT = 16
    P = 0.25

    def __init__(self):
        self.head = _Node(None, self.MAX_HEIGHT)
        self.height = 1

    def __iter__(self):
        """(key, ids) pairs in order."""
        node = self.head.next[0]
        while node is not None:
            yield node.key, node.ids
            node = node.next[0]

    def lane_keys(self, lane: int) -> list:
        keys, node = [], self.head.next[lane]
        while node is not None:
            keys.append(node.key)
            node = node.next[lane]
        return keys

    def _path(self, key, trace=None) -> list[_Node]:
        """The last node before `key` in each lane."""
        path = [self.head] * self.MAX_HEIGHT
        node = self.head
        for lane in range(self.height - 1, -1, -1):
            nxt = node.next[lane]
            while nxt is not None:
                if trace is not None:
                    trace[nxt.key] = nxt.ids
                if not nxt.key < key:
                    break
                node, nxt = nxt, nxt.next[lane]
            path[lane] = node
        return path

    def get(self, key) -> set[int]:
        """Ids of the cells holding a value equal to `key` (an empty set if none)."""
        node = self.head
        for lane in range(self.height - 1, -1, -1):
            nxt = node.next[lane]
            while nxt is not None and nxt.key < key:
                node, nxt = nxt, nxt.next[lane]
        node = node.next[0]
        return node.ids if node is not None and node.key == key else set()

    def get_with_trace(self, key):
        trace = {}
        path = self._path(key, trace)
        node = path[0].next[0]
        return (node.ids if node is not None and node.key == key else set()), trace

    def between(self, low=None, high=None, low_inclusive=True, high_inclusive=True, limit=-1):
        """Id sets of the keys between two bounds (None: unbounded), in order. None if more than `limit` ids match."""
        if low is None:
            node = self.head.next[0]
        else:
            node = self._path(low)[0].next[0]
            if node is not None and not low_inclusive and node.key == low:
                node = node.next[0]
        out, total = [], 0
        while node is not None:
            if high is not None and not (node.key <= high if high_inclusive else node.key < high):
                break
            total += len(node.ids)
            if 0 <= limit < total:
                return None
            out.append(node.ids)
            node = node.next[0]
        return out

    def nearest(self, bound=None, count=1, descending=False, inclusive=True):
        """(key, ids) pairs in key order from a bound (None: the first or last key), until they hold `count` ids.
        Lanes link forward only, so a descending walk repeats the predecessor search."""
        if bound is None:
            node = self.head
            if descending:
                for lane in range(self.height - 1, -1, -1):
                    while node.next[lane] is not None:
                        node = node.next[lane]
            else:
                node = node.next[0]
        else:
            before = self._path(bound)[0]
            node = before.next[0]
            equal = node is not None and node.key == bound
            if descending:
                node = node if equal and inclusive else before
            elif equal and not inclusive:
                node = node.next[0]
        out, total = [], 0
        while node is not None and node is not self.head and total < count:
            out.append((node.key, node.ids))
            total += len(node.ids)
            node = self._path(node.key)[0] if descending else node.next[0]
        return out

    def insert(self, key, cell_id: int):
        path = self._path(key)
        node = path[0].next[0]
        if node is not None and node.key == key:
            node.ids.add(cell_id)
            return
        height = 1
        while height < self.MAX_HEIGHT and random.random() < self.P:
            height += 1
        self.height = max(self.height, height)
        node = _Node(key, height)
        node.ids.add(cell_id)
        for lane in range(height):
            node.next[lane] = path[lane].next[lane]
            path[lane].next[lane] = node

    def get_many(self, keys, chained=True, trace=False):
        """Exact lookups with the same batch-local finger as the native implementation."""
        path = [self.head] * self.MAX_HEIGHT
        previous = None
        out = []
        visited = {} if trace else None
        for i, key in enumerate(keys):
            if not chained or i == 0 or key < previous:
                path = self._path(key, visited)
            else:
                top = 0
                while top < self.height - 1:
                    nxt = path[top].next[top]
                    if visited is not None and nxt is not None:
                        visited[nxt.key] = nxt.ids
                    if nxt is None or not nxt.key < key:
                        break
                    if top == 2:
                        top = self.height - 1
                        break
                    top += 1
                node = path[top]
                for lane in range(top, -1, -1):
                    saved = path[lane]
                    if saved is not self.head and node is not saved:
                        if node is self.head or node.key < saved.key:
                            node = saved
                    nxt = node.next[lane]
                    while nxt is not None:
                        if visited is not None:
                            visited[nxt.key] = nxt.ids
                        if not nxt.key < key:
                            break
                        node, nxt = nxt, nxt.next[lane]
                    path[lane] = node
            node = path[0].next[0]
            out.append(node.ids if node is not None and node.key == key else set())
            previous = key
        return (out, visited) if trace else out

    def remove(self, key, cell_id: int):
        """Raises KeyError if the cell isn't filed under `key`."""
        path = self._path(key)
        node = path[0].next[0]
        if node is None or node.key != key:
            raise KeyError(key)
        node.ids.remove(cell_id)
        if node.ids:
            return
        for lane in range(len(node.next)):
            path[lane].next[lane] = node.next[lane]
        while self.height > 1 and self.head.next[self.height - 1] is None:
            self.height -= 1


def py_scan(cells: list, op: int, value, value_first: bool = False) -> list[int]:
    """Indices of the cells whose value matches. Values that can't be compared (TypeError) don't match."""
    if op == IS_NULL:
        return [i for i, cell in enumerate(cells) if cell._value is None]
    if op == NOT_NULL:
        return [i for i, cell in enumerate(cells) if cell._value is not None]
    compare = _COMPARE[op]
    out = []
    for i, cell in enumerate(cells):
        try:
            if compare(value, cell._value) if value_first else compare(cell._value, value):
                out.append(i)
        except TypeError:
            pass
    return out


def py_scan_match(cells: list, fullmatch, negate: bool = False) -> list[int]:
    """Indices of the string cells where `bool(fullmatch(value)) != negate`."""
    return [i for i, cell in enumerate(cells) if isinstance(cell._value, str) and bool(fullmatch(cell._value)) != negate]


def py_values(cells: list) -> list:
    return [cell._value for cell in cells]


SkipList, scan, scan_match, values = PySkipList, py_scan, py_scan_match, py_values
SPEEDUPS = False
if os.environ.get("SIQL_PURE_PYTHON") != "1":
    try:
        from .. import _speedups
    except ImportError:
        pass
    else:
        SkipList, scan, scan_match, values = _speedups.SkipList, _speedups.scan, _speedups.scan_match, _speedups.values
        SPEEDUPS = True
