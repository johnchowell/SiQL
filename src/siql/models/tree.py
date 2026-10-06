"""Search tree for a Table, in layers: column name -> first character/digit of a value -> the cells holding it."""
import numbers
from typing import TYPE_CHECKING

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


class ColumnTree():
    """One column's layer of a Table's tree.

    `branches[key]` maps `id(cell)` to `(row, cell)`, where `row` is the Row object in `table.rows` and `cell`
    is the Cell object shared by that row and `table.cols[column]`. Cells notify the tree when their value
    changes, so it stays current however the value is set.

    With lazy deletes, a deleted row's cell is queued under its branch instead of being unlinked straight away.
    A search unlinks the queued cells of just the branches it reads, before reading them.
    """
    def __init__(self):
        self.branches: dict[str | None, dict[int, tuple["Row", "Cell"]]] = {}
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
        self.branches.setdefault(key, {})[id(cell)] = (row, cell)
        self._unqueue(key, id(cell))
        cell._tree = self

    def remove(self, cell: "Cell"):
        key = tree_key(cell.value)
        self._pop(key, cell)
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
                    branch.pop(cell_id)[1]._tree = None
                self._dead_count -= len(queued)
                if not branch:
                    del self.branches[key]
            if not purge_only:
                entries.extend(self.branches.get(key, {}).values())
        return entries

    def _pop(self, key, cell):
        branch = self.branches[key]
        entry = branch.pop(id(cell))
        if not branch:
            del self.branches[key]
        return entry

    def changed(self, cell: "Cell", old):
        """Move a cell to the branch for its new value. Called by the cell itself."""
        old_key, new_key = tree_key(old), tree_key(cell.value)
        if old_key != new_key:
            self.branches.setdefault(new_key, {})[id(cell)] = self._pop(old_key, cell)
            if self._unqueue(old_key, id(cell)):  # a deleted row's cell changed; keep it queued with its branch
                self._dead.setdefault(new_key, set()).add(id(cell))
                self._dead_count += 1

    def find(self, value) -> list["Row"]:
        """Rows (in no particular order) whose value in this column equals `value`."""
        key = tree_key(value)
        if key is None and value is not None:
            # An unusual value might compare equal to anything, so check every branch
            keys = list(self.branches)
        elif key is None:
            keys = [None]
        else:
            # Unkeyed values (custom objects, nan, ...) live in the None branch and might still be equal
            keys = [key, None]
        return [row for row, cell in self._live(keys) if value == cell.value]
