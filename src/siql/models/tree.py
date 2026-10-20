"""Search tree for a Table, in layers: column name -> first character/digit of a value -> the cells holding it."""
import numbers
import os
from typing import TYPE_CHECKING

from ..helpers.search import SkipList
from .cache import cacheable

if TYPE_CHECKING:
    from .row import Row, Cell

_DIRECT_REFS = os.environ.get("SIQL_DIRECT_REFS", "1") != "0"


def tree_key(value) -> str | None:
    """Branch for a value: the first character of a string, or the first digit of a real number's whole part
    (so 1, 1.0 and True share a branch, as they're equal). Anything else, including nan and inf, is None."""
    if isinstance(value, str):
        return value[:1]
    if isinstance(value, numbers.Real):
        try:
            return str(abs(int(value)))[0]
        except (OverflowError, ValueError):
            return None
    return None


def order_key(value) -> tuple:
    """Sort key for a value in a keyed branch. Numbers sort before strings, so the two are never compared."""
    return (1, value) if isinstance(value, str) else (0, value)


class Branch(dict):
    """One branch: `id(cell)` -> `(row, cell)` for every cell in it. String and number branches also keep their
    values in a SkipList, so a lookup can jump to a value; other values (None, lists, nan, ...) can't be ordered
    and are checked one by one."""
    __slots__ = ("lanes", "_direct")

    def __init__(self, ordered: bool):
        super().__init__()
        self.lanes = SkipList() if ordered else None
        self._direct = {} if ordered and _DIRECT_REFS else None

    def _direct_put(self, row: "Row", cell: "Cell"):
        if self._direct is None or not cacheable(cell.value):
            return
        key = cell.value
        entry = self._direct.get(key)
        if entry is None:
            self._direct[key] = (cell, row)
        elif isinstance(entry, dict):
            entry[cell] = row
        elif entry[0] is cell:
            self._direct[key] = (cell, row)
        else:
            self._direct[key] = {entry[0]: entry[1], cell: row}

    def _direct_remove(self, cell: "Cell", value):
        if self._direct is None or not cacheable(value):
            return
        key = value
        entry = self._direct.get(key)
        if isinstance(entry, dict):
            entry.pop(cell, None)
            if len(entry) == 1:
                self._direct[key] = next(iter(entry.items()))
            elif not entry:
                del self._direct[key]
        elif entry is not None and entry[0] is cell:
            del self._direct[key]

    def _direct_rows(self, value) -> list["Row"]:
        entry = self._direct.get(value)
        return list(entry.values()) if isinstance(entry, dict) else ([entry[1]] if entry is not None else [])

    def _refresh(self, cell: "Cell", old):
        """Keep the direct index current when a cell changes to an equal value."""
        self._direct_remove(cell, old)
        self._direct_put(self[id(cell)][0], cell)

    def put(self, row: "Row", cell: "Cell"):
        if self.lanes is not None and id(cell) not in self:
            self.lanes.insert(order_key(cell.value), id(cell))
        self[id(cell)] = (row, cell)
        self._direct_put(row, cell)

    def take(self, cell_id: int, value) -> tuple["Row", "Cell"]:
        """Remove a cell that was filed under `value`."""
        if self.lanes is not None:
            self.lanes.remove(order_key(value), cell_id)
        row, cell = self.pop(cell_id)
        self._direct_remove(cell, value)
        return row, cell

    def matching(self, value) -> list[tuple["Row", "Cell"]]:
        if self.lanes is None:
            return [entry for entry in self.values() if value == entry[1].value]
        return [self[cell_id] for cell_id in self.lanes.get(order_key(value))]


class ColumnTree():
    """One column's layer of a Table's tree.

    `branches[key]` is a Branch mapping `id(cell)` to `(row, cell)`, where `row` is the Row object in `table.rows`
    and `cell` is the Cell object shared by that row and `table.cols[column]`. Cells notify the tree when their
    value changes, so it stays current however the value is set.

    With lazy deletes, a deleted row's cell is queued under its branch instead of being unlinked straight away.
    A search unlinks the queued cells of just the branches it reads, before reading them.
    Lazy additions retain cells in a pending map, inserting them into lanes when their branches are read.
    """
    def __init__(self, *, cache=None, observe=True, on_change=None):
        self._observe = observe
        self._on_change = on_change
        self._cache = cache
        self._cache_column = object()
        self._uncacheable = set()
        self.branches: dict[str | None, Branch] = {}
        self._dead: dict[str | None, set[int]] = {}  # branch key -> ids of queued cells in that branch
        self._dead_count = 0
        self._pending = {}  # branch key -> cell id -> (row, cell), not yet in skip-list lanes
        self._pending_count = 0

    @property
    def dead(self) -> set[int]:
        """Ids of every queued cell."""
        return set().union(*self._dead.values())

    @property
    def pending(self) -> set[int]:
        """Cell IDs waiting for lazy insertion into this column's tree."""
        return set().union(*(entries.keys() for entries in self._pending.values()))

    def __len__(self):
        return sum(len(b) for b in self.branches.values()) - self._dead_count + self._pending_count

    def __getitem__(self, key: str | None) -> list["Row"]:
        """Rows whose value in this column falls in a branch."""
        return [row for row, _ in self._live([key])]

    def add(self, row: "Row", cell: "Cell"):
        self.clear_cache()
        self._pop_pending(tree_key(cell.value), id(cell))
        if not cacheable(cell.value):
            self._uncacheable.add(id(cell))
        key = tree_key(cell.value)
        branch = self.branches.get(key)
        if branch is None:
            branch = self.branches[key] = Branch(ordered=key is not None)
        branch.put(row, cell)
        self._unqueue(key, id(cell))
        if self._observe:
            cell._tree = self

    def queue_add(self, row: "Row", cell: "Cell"):
        """Defer lane insertion, retaining ownership so pending edits stay observable."""
        key, cell_id = tree_key(cell.value), id(cell)
        if cell_id in self.branches.get(key, {}):
            # Undoing a lazy delete revives the entry already in the lanes.
            self.add(row, cell)
            return
        self.clear_cache()
        if not cacheable(cell.value):
            self._uncacheable.add(cell_id)
        entries = self._pending.setdefault(key, {})
        if cell_id not in entries:
            self._pending_count += 1
        entries[cell_id] = (row, cell)
        if self._observe:
            cell._tree = self
        indexed = len(self) - self._pending_count
        if self._pending_count > max(64, indexed):
            self.flush_pending()

    def _pop_pending(self, key, cell_id):
        entries = self._pending.get(key)
        if entries is None or cell_id not in entries:
            return None
        entry = entries.pop(cell_id)
        self._pending_count -= 1
        if not entries:
            del self._pending[key]
        return entry

    def flush_pending(self, keys=None):
        """Index queued additions for selected branches, or every branch when omitted."""
        for key in list(self._pending) if keys is None else keys:
            entries = self._pending.pop(key, None)
            if not entries:
                continue
            branch = self.branches.get(key)
            if branch is None:
                branch = self.branches[key] = Branch(ordered=key is not None)
            for row, cell in entries.values():
                branch.put(row, cell)
            self._pending_count -= len(entries)

    def remove(self, cell: "Cell"):
        self.clear_cache()
        self._uncacheable.discard(id(cell))
        key = tree_key(cell.value)
        if self._pop_pending(key, id(cell)) is not None:
            if self._observe:
                cell._tree = None
            return
        self._take(key, id(cell), cell.value)
        self._unqueue(key, id(cell))
        if self._observe:
            cell._tree = None

    def bury(self, cell: "Cell"):
        """Queue a deleted row's cell to be unlinked by a later search of its branch."""
        if id(cell) in self._pending.get(tree_key(cell.value), {}):
            self.remove(cell)
            return
        self.clear_cache()
        self._uncacheable.discard(id(cell))
        self._dead.setdefault(tree_key(cell.value), set()).add(id(cell))
        self._dead_count += 1
        if self._dead_count > max(64, len(self)):  # keep the queue from outgrowing the live entries
            self.sweep()

    def sweep(self):
        """Finish all queued additions and deletions now."""
        self._live(list(set(self._dead) | set(self._pending)), purge_only=True)

    def _unqueue(self, key, cell_id: int) -> bool:
        queued = self._dead.get(key)
        if not queued or cell_id not in queued:
            return False
        queued.remove(cell_id)
        if not queued:
            del self._dead[key]
        self._dead_count -= 1
        return True

    def _live(self, keys, purge_only=False) -> list[tuple["Row", "Cell"]]:
        """Entries of the given branches, first unlinking any cells queued in them."""
        entries = []
        for key in keys:
            queued = self._dead.pop(key, None)
            if queued:
                branch = self.branches[key]
                for cell_id in queued:
                    cell = branch.take(cell_id, branch[cell_id][1].value)[1]
                    if self._observe:
                        cell._tree = None
                self._dead_count -= len(queued)
                if not branch:
                    del self.branches[key]
            self.flush_pending([key])
            if not purge_only:
                entries.extend(self.branches.get(key, {}).values())
        return entries

    def _take(self, key, cell_id: int, value):
        branch = self.branches[key]
        entry = branch.take(cell_id, value)
        if not branch:
            del self.branches[key]
        return entry

    def changed(self, cell: "Cell", old):
        """Refile a cell under its new value. Called by the cell itself."""
        new = cell.value
        old_key, new_key = tree_key(old), tree_key(new)
        pending = self._pending.get(old_key, {}).get(id(cell))
        if self._on_change is not None:
            self._on_change((pending if pending is not None else self.branches[old_key][id(cell)])[0], cell)
        self.clear_cache()
        if cacheable(new) or id(cell) in self._dead.get(old_key, ()):
            self._uncacheable.discard(id(cell))
        else:
            self._uncacheable.add(id(cell))
        if pending is not None:
            self._pop_pending(old_key, id(cell))
            self._pending.setdefault(new_key, {})[id(cell)] = pending
            self._pending_count += 1
            return
        if old_key == new_key:
            if old_key is not None and order_key(old) != order_key(new):
                branch = self.branches[old_key]
                branch.put(*branch.take(id(cell), old))
            elif old_key is not None:
                self.branches[old_key]._refresh(cell, old)
            return
        entry = self._take(old_key, id(cell), old)
        branch = self.branches.get(new_key)
        if branch is None:
            branch = self.branches[new_key] = Branch(ordered=new_key is not None)
        branch.put(*entry)
        if self._unqueue(old_key, id(cell)):  # a deleted row's cell changed; keep it queued with its branch
            self._dead.setdefault(new_key, set()).add(id(cell))
            self._dead_count += 1

    def clear_cache(self):
        if self._cache is not None:
            self._cache.invalidate(self._cache_column)

    def _can_cache(self, value):
        return (self._cache is not None and self._cache.max_bytes > 0
                and not self._uncacheable and cacheable(value))

    def _remember_passed(self, branch, visited):
        self._cache.traversal_nodes_seen += len(visited)
        for key, cell_ids in visited.items():
            rows = [branch[cell_id][0] for cell_id in cell_ids]
            if self._cache.put(self._cache_column, key[1], rows):
                self._cache.traversal_admissions += 1

    def find(self, value) -> list["Row"]:
        """Use a live-row cache before the ordinary exact tree lookup."""
        use_cache = self._can_cache(value)
        if use_cache:
            rows = self._cache.get(self._cache_column, value)
            if rows is not None:
                return list(rows)
        rows = self._find(value)
        if use_cache:
            self._cache.put(self._cache_column, value, rows)
        return rows

    def _find(self, value) -> list["Row"]:
        """Rows (in no particular order) whose value in this column equals `value`."""
        key = tree_key(value)
        if key is None:
            # An unusual value might compare equal to anything, so check every branch (None only checks its own)
            keys = [None] if value is None else list(set(self.branches) | set(self._pending))
            return [row for row, cell in self._live(keys) if value == cell.value]
        # Unkeyed values (custom objects, nan, ...) live in the None branch and might still be equal
        self._live([key, None], purge_only=True)
        branch = self.branches.get(key)
        if (branch is not None and branch._direct is not None and not self._uncacheable
                and cacheable(value) and not (self._cache is not None and self._cache.traversed_nodes)):
            rows = branch._direct_rows(value)
        elif (branch is not None and self._cache is not None and self._cache.traversed_nodes
                and self._can_cache(value)):
            cell_ids, visited = branch.lanes.get_with_trace(order_key(value))
            self._remember_passed(branch, visited)
            rows = [branch[cell_id][0] for cell_id in cell_ids]
        else:
            rows = [row for row, _ in branch.matching(value)] if branch is not None else []
        if None in self.branches and (self._uncacheable or not cacheable(value)):
            rows += [row for row, _ in self.branches[None].matching(value)]
        return rows

    def find_batch(self, values, *, order="tree", chained=True):
        """Batch-local fingers per ordered branch; restore input order after scheduling queries."""
        if order not in ("tree", "input"):
            raise ValueError("order must be 'tree' or 'input'")
        values = list(values)
        out = [None] * len(values)
        groups = {}
        for i, value in enumerate(values):
            if self._can_cache(value):
                cached = self._cache.get(self._cache_column, value)
                if cached is not None:
                    out[i] = list(cached)
                    continue
            key = tree_key(value)
            if key is None:
                out[i] = self._find(value)
            else:
                groups.setdefault(key, []).append(i)
        for branch_key, indices in groups.items():
            self._live([branch_key, None], purge_only=True)
            branch = self.branches.get(branch_key)
            direct = (branch is not None and branch._direct is not None and not self._uncacheable
                      and all(cacheable(values[i]) for i in indices)
                      and not (self._cache is not None and self._cache.traversed_nodes))
            if direct:
                for i in indices:
                    out[i] = branch._direct_rows(values[i])
                continue
            if order == "tree":
                indices.sort(key=lambda i: order_key(values[i]))
            # Sorting a few sparse queries is useful for grouping, but a fresh seek can
            # cost less than finger maintenance across large gaps. Keep the cheap route.
            reuse = chained
            if order == "tree" and branch is not None:
                reuse = chained and len(indices) >= 8 and len(indices) * 64 >= len(branch)
            if branch is None:
                matches = [set() for _ in indices]
            else:
                # Mixed/custom queries must not turn an otherwise uncached batch into a traced one.
                tracing = (self._cache is not None and self._cache.traversed_nodes
                           and all(self._can_cache(values[i]) for i in indices))
                found = branch.lanes.get_many([order_key(values[i]) for i in indices], chained=reuse,
                                              trace=tracing)
                if tracing:
                    matches, visited = found
                    self._remember_passed(branch, visited)
                else:
                    matches = found
            unkeyed = self.branches.get(None)
            for i, cell_ids in zip(indices, matches):
                rows = [branch[cell_id][0] for cell_id in cell_ids] if branch is not None else []
                if unkeyed is not None:
                    rows.extend(row for row, _ in unkeyed.matching(values[i]))
                out[i] = rows
        # Recency follows the user's query order, not the scheduler's sorted traversal order.
        for value, rows in zip(values, out):
            if self._can_cache(value):
                self._cache.put(self._cache_column, value, rows)
        return out
