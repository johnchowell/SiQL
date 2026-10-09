"""Search tree for a Table, in layers: column name -> first character/digit of a value -> the cells holding it."""
import numbers
from typing import TYPE_CHECKING

from ..helpers.search import SkipList

if TYPE_CHECKING:
    from .row import Row, Cell


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
    __slots__ = ("lanes",)

    def __init__(self, ordered: bool):
        super().__init__()
        self.lanes = SkipList() if ordered else None

    def put(self, row: "Row", cell: "Cell"):
        if self.lanes is not None and id(cell) not in self:
            self.lanes.insert(order_key(cell.value), id(cell))
        self[id(cell)] = (row, cell)

    def take(self, cell_id: int, value) -> tuple["Row", "Cell"]:
        """Remove a cell that was filed under `value`."""
        if self.lanes is not None:
            self.lanes.remove(order_key(value), cell_id)
        return self.pop(cell_id)

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
    """
    def __init__(self):
        self.branches: dict[str | None, Branch] = {}
        self._dead: dict[str | None, set[int]] = {}  # branch key -> ids of queued cells in that branch
        self._dead_count = 0

    @property
    def dead(self) -> set[int]:
        """Ids of every queued cell."""
        return set().union(*self._dead.values())

    def __len__(self):
        return sum(len(b) for b in self.branches.values()) - self._dead_count

    def __getitem__(self, key: str | None) -> list["Row"]:
        """Rows whose value in this column falls in a branch."""
        return [row for row, _ in self._live([key])]

    def add(self, row: "Row", cell: "Cell"):
        key = tree_key(cell.value)
        branch = self.branches.get(key)
        if branch is None:
            branch = self.branches[key] = Branch(ordered=key is not None)
        branch.put(row, cell)
        self._unqueue(key, id(cell))
        cell._tree = self

    def remove(self, cell: "Cell"):
        key = tree_key(cell.value)
        self._take(key, id(cell), cell.value)
        self._unqueue(key, id(cell))
        cell._tree = None

    def bury(self, cell: "Cell"):
        """Queue a deleted row's cell to be unlinked by a later search of its branch."""
        self._dead.setdefault(tree_key(cell.value), set()).add(id(cell))
        self._dead_count += 1
        if self._dead_count > max(64, len(self)):  # keep the queue from outgrowing the live entries
            self.sweep()

    def sweep(self):
        """Unlink every queued cell now."""
        self._live(list(self._dead), purge_only=True)

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
                    branch.take(cell_id, branch[cell_id][1].value)[1]._tree = None
                self._dead_count -= len(queued)
                if not branch:
                    del self.branches[key]
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
        if old_key == new_key:
            if old_key is not None and order_key(old) != order_key(new):
                branch = self.branches[old_key]
                branch.put(*branch.take(id(cell), old))
            return
        entry = self._take(old_key, id(cell), old)
        branch = self.branches.get(new_key)
        if branch is None:
            branch = self.branches[new_key] = Branch(ordered=new_key is not None)
        branch.put(*entry)
        if self._unqueue(old_key, id(cell)):  # a deleted row's cell changed; keep it queued with its branch
            self._dead.setdefault(new_key, set()).add(id(cell))
            self._dead_count += 1

    def find(self, value) -> list["Row"]:
        """Rows (in no particular order) whose value in this column equals `value`."""
        key = tree_key(value)
        if key is None:
            # An unusual value might compare equal to anything, so check every branch (None only checks its own)
            keys = [None] if value is None else list(self.branches)
            return [row for row, cell in self._live(keys) if value == cell.value]
        # Unkeyed values (custom objects, nan, ...) live in the None branch and might still be equal
        self._live([key, None], purge_only=True)
        rows = [row for row, _ in self.branches[key].matching(value)] if key in self.branches else []
        if None in self.branches:
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
            key = tree_key(value)
            if key is None:
                out[i] = self.find(value)
            else:
                groups.setdefault(key, []).append(i)
        for branch_key, indices in groups.items():
            self._live([branch_key, None], purge_only=True)
            if order == "tree":
                indices.sort(key=lambda i: order_key(values[i]))
            branch = self.branches.get(branch_key)
            # Sorting a few sparse queries is useful for grouping, but a fresh seek can
            # cost less than finger maintenance across large gaps. Keep the cheap route.
            reuse = chained
            if order == "tree" and branch is not None:
                reuse = chained and len(indices) >= 8 and len(indices) * 64 >= len(branch)
            matches = (branch.lanes.get_many([order_key(values[i]) for i in indices], chained=reuse)
                       if branch is not None else [set() for _ in indices])
            unkeyed = self.branches.get(None)
            for i, cell_ids in zip(indices, matches):
                rows = [branch[cell_id][0] for cell_id in cell_ids] if branch is not None else []
                if unkeyed is not None:
                    rows.extend(row for row, _ in unkeyed.matching(values[i]))
                out[i] = rows
        return out
