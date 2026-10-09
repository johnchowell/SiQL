import bisect
import io
import os
import uuid
from typing import Type, Self

from .row import RowStruct, Cell, Row
from .diff import TableDiff, DiffOp, AddCol, AddRow, SetCell, commit_line
from .tree import ColumnTree
from ..helpers.format import box
from ..helpers.search import scan, EQ

FILE_ENCODING = "utf-8"


class Table():
    rows:list[Row]
    cols:dict[str, list[Cell]]
    struct:RowStruct
    index:dict[str, ColumnTree] | None
    _file:str

    @property
    def entries(self) -> list[Row]:
        return self.rows

    def __init__(self, t:list[Row] | None = None, *, file=None, tree:bool = True, lazy_delete:bool = False):
        """
        Args:
            t: initial rows (only allowed when the file is new or empty)
            file: table file path. An existing file is loaded by replaying its logged changes.
            tree: keep a search tree (`index`) of every column, used by `find` and `select`
            lazy_delete: with the tree on, queue the tree cleanup for deleted rows instead of doing it at once.
                `rows`, `cols` and the file are still updated immediately; searches unlink queued rows as
                they pass them, and `purge()` finishes the queue.
        """
        self.rows = []
        self.struct = RowStruct()
        self.cols = {}
        self.index = None
        self.lazy_delete = lazy_delete
        self._positions = None  # id(row) -> position when the map was built, used by `find`
        self._deleted = []  # sorted map positions of rows deleted since then (the lazy delete queue)
        self._file = (file if file is not None else f"{uuid.uuid4().hex}.siql")
        self._buffer = io.StringIO()

        if os.path.exists(self._file):
            self._load()

        if t:
            if len(self.struct) or self.rows:
                raise ValueError(f"Can't pass initial rows for an existing table file: {self._file}")
            self.rows = t
            self.struct = t[0].struct
            # Each column list holds the same Cell objects as the rows, so edits are visible both ways
            self.cols = {name: [r.col(name) for r in self.rows] for name in self.struct.column_names}
            initial = [AddCol(n, ty, i) for i, (n, ty) in enumerate(zip(self.struct.column_names, self.struct.columns))]
            initial += [AddRow(i, {n: r.col(n).value for n in self.struct.column_names}) for i, r in enumerate(self.rows)]
            self._buffer.write(commit_line(initial))
        self.save()
        self.tree = tree

    @property
    def tree(self) -> bool:
        """Whether the table keeps a search tree. Setting it builds or discards the tree."""
        return self.index is not None

    @tree.setter
    def tree(self, enabled: bool):
        if enabled and self.index is None:
            self.index = {}
            for name in self.struct.column_names:
                self._index_column(name)
        elif not enabled and self.index is not None:
            for node in self.index.values():
                node.sweep()
            for cells in self.cols.values():
                for cell in cells:
                    cell._tree = None
            self.index = None

    def purge(self):
        """Finish all queued lazy deletes now."""
        for node in (self.index or {}).values():
            node.sweep()
        self._forget_positions()

    def _forget_positions(self):
        self._positions = None
        self._deleted.clear()

    def _index_column(self, name:str):
        node = self.index[name] = ColumnTree()
        for row, cell in zip(self.rows, self.cols[name]):
            node.add(row, cell)

    def _row_added(self, row:Row, index:int):
        if self._positions is not None and index == len(self.rows) - 1:
            # Map positions run past the queued deletes, so an appended row goes after all of them
            self._positions[id(row)] = index + len(self._deleted)
        else:
            self._forget_positions()  # rows after `index` shifted
        if self.index is not None:
            for name, node in self.index.items():
                node.add(row, row.col(name))

    def _row_removed(self, row:Row, index:int):
        if self._positions is None:
            pass
        elif self.lazy_delete and len(self._deleted) < len(self.rows) // 4 + 64:
            bisect.insort(self._deleted, self._positions.pop(id(row)))
        elif index == len(self.rows) and not self._deleted:
            del self._positions[id(row)]
        else:
            self._forget_positions()
        if self.index is not None:
            for name, node in self.index.items():
                if self.lazy_delete:
                    node.bury(row.col(name))
                else:
                    node.remove(row.col(name))

    def _load(self):
        """Replay the table file. A trailing partial line (from a write cut off by a crash) is discarded
        and truncated away so later appends start on a clean line."""
        with open(self._file, "r+b") as f:
            data = f.read()
            end = data.rfind(b"\n") + 1
            if end < len(data):
                f.truncate(end)
        for op in TableDiff.loads(data[:end].decode(FILE_ENCODING)):
            op.apply(self)

    def save(self):
        """Flush buffered changes to the end of the table file."""
        data = self._buffer.getvalue()
        if data or not os.path.exists(self._file):
            with open(self._file, "a", encoding=FILE_ENCODING) as f:
                f.write(data)
        self._buffer.seek(0)
        self._buffer.truncate()

    def compact(self):
        """Rewrite the table file as one snapshot of the current table, dropping its change history.
        The new file replaces the old one in a single step, so a crash leaves one or the other intact.
        """
        self.save()
        names = self.struct.column_names
        ops = [AddCol(n, t, i) for i, (n, t) in enumerate(zip(names, self.struct.columns))]
        ops += [AddRow(i, {n: r.col(n).value for n in names}) for i, r in enumerate(self.rows)]
        tmp = self._file + ".tmp"
        with open(tmp, "w", encoding=FILE_ENCODING) as f:
            f.write(commit_line(ops) if ops else "")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._file)

    def _commit(self, ops:list[DiffOp]):
        """Apply ops as one all-or-nothing change.
        The whole change is serialized into a single buffered line before anything is applied. If any op
        fails, the ones already applied are rolled back and nothing is written. Otherwise the line is saved
        in one write; a crash mid-write leaves a partial last line, which is dropped on load.
        """
        if not ops:
            return
        line = commit_line(ops)  # raises TypeError for unwritable values before anything changes
        undo = []
        try:
            for op in ops:
                undo.append(op.apply(self))
        except Exception:
            for u in reversed(undo):
                u()
            raise
        self._buffer.write(line)
        self.save()

    # Low-level mutations used by DiffOps. They don't log (go through _commit for that) and either
    # raise before changing anything or return a function that undoes the change.
    def _insert_col(self, name:str, type:Type, index:int):
        if name in self.cols:
            raise ValueError(f"Column already exists: {name}")
        for r in self.rows:
            r._fill()
        self.struct.columns.insert(index, type)
        self.struct.column_names.insert(index, name)
        for r in self.rows:
            r.content.insert(index, Cell())
        self.cols[name] = [r.col(name) for r in self.rows]
        if self.index is not None:
            self._index_column(name)
        return lambda: self._drop_col(name)

    def _drop_col(self, name:str):
        i = self.struct.col(name)
        for r in self.rows:
            r._fill()
        type = self.struct.columns.pop(i)
        self.struct.column_names.pop(i)
        removed = [r.content.pop(i) for r in self.rows]
        cells = self.cols.pop(name)
        if self.index is not None:
            self.index.pop(name).sweep()
            for cell in cells:
                cell._tree = None

        def undo():
            self.struct.columns.insert(i, type)
            self.struct.column_names.insert(i, name)
            for r, c in zip(self.rows, removed):
                r.content.insert(i, c)
            self.cols[name] = cells
            if self.index is not None:
                self._index_column(name)
        return undo

    def _set_col_type(self, name:str, type:Type):
        i = self.struct.col(name)
        old = self.struct.columns[i]
        self.struct.columns[i] = type
        return lambda: self.struct.columns.__setitem__(i, old)

    def _rename_col(self, name:str, new_name:str):
        i = self.struct.col(name)
        if new_name in self.cols:
            raise ValueError(f"Column already exists: {new_name}")
        self.struct.column_names[i] = new_name
        self.cols = {(new_name if k == name else k): v for k, v in self.cols.items()}
        if self.index is not None:
            self.index[new_name] = self.index.pop(name)
        return lambda: self._rename_col(new_name, name)

    def _insert_row(self, index:int, values:dict):
        r = Row(self.struct)
        for name, value in values.items():
            r.col(name).value = value
        n = len(self.rows)
        index = min(index if index >= 0 else max(0, n + index), n)  # same position list.insert would use
        self.rows.insert(index, r)
        for name in self.struct.column_names:
            self.cols[name].insert(index, r.col(name))
        self._row_added(r, index)
        return lambda: self._drop_row(index)

    def _drop_row(self, index:int):
        r = self.rows.pop(index)
        index = index % (len(self.rows) + 1)  # normalize negative indices for undo
        for cells in self.cols.values():
            cells.pop(index)
        self._row_removed(r, index)

        def undo():
            self.rows.insert(index, r)
            for name in self.struct.column_names:
                self.cols[name].insert(index, r.col(name))
            self._row_added(r, index)
        return undo

    def _set_cell(self, row:int, col:str, value):
        cell = self.rows[row].col(col)
        old = cell.value
        cell.value = value
        return lambda: setattr(cell, "value", old)

    def column(self, name: str | int):
        """Yield cells in a column
        Args:
            name: column name or index
        """
        if isinstance(name, int):
            name = self.struct.column_names[name]
        yield from self.cols[name]

    # Builds a nice little print table
    def __str__(self):
        names = self.struct.column_names
        if not names:
            return ""

        head = [f"{n}: {t.__name__}" for n, t in zip(names, self.struct.columns)]
        body = [[str(row.col(j)) for j in range(len(names))] for row in self.rows]
        return box(head, body)

    def select(self, var, col_name=None):
        """Select rows by value
        Args:
            var: value to match
            col_name: column to check (default: every column whose type is the type of `var`)
        """
        if col_name is None:
            t:Type = type(var)
            for i in self.struct:
                if self.struct.columns[i] == t:
                    for j in self.find(i, var):
                        yield self.rows[j]
        else:
            for j in self.find(col_name, var):
                yield self.rows[j]

    def find(self, col_name: str | int, value) -> list[int]:
        """Indices, in order, of the rows whose cell in a column equals `value`.
        Uses the search tree when `tree` is on, otherwise checks every row.
        """
        if isinstance(col_name, int):
            col_name = self.struct.column_names[col_name]
        if self.index is None:
            return scan(self.cols[col_name], EQ, value, True)
        rows = self.index[col_name].find(value)
        if not rows:
            return []
        if self._positions is None:
            self._positions = {id(r): i for i, r in enumerate(self.rows)}
            self._deleted.clear()
        found = sorted(self._positions[id(r)] for r in rows)
        if self._deleted:
            # Each queued delete before a row moved it up one place
            return [p - bisect.bisect_left(self._deleted, p) for p in found]
        return found

    def find_batch(self, col_name, values, *, order="tree", chained=True):
        """Exact lookup for each input, scheduling nearby queries and reusing tree search paths.

        Return one list of row indices per input, in the original input order. 'tree' uses
        the index's actual key order; 'input' skips sorting.
        With the tree disabled this uses independent exact scans.
        """
        if order not in ("tree", "input"):
            raise ValueError("order must be 'tree' or 'input'")
        if isinstance(col_name, int):
            col_name = self.struct.column_names[col_name]
        values = list(values)
        if self.index is None:
            cells = self.cols[col_name]
            return [scan(cells, EQ, value, True) for value in values]
        matches = self.index[col_name].find_batch(values, order=order, chained=chained)
        if not any(matches):
            return [[] for _ in values]
        if self._positions is None:
            self._positions = {id(row): i for i, row in enumerate(self.rows)}
            self._deleted.clear()
        result = [sorted(self._positions[id(row)] for row in rows) for rows in matches]
        if self._deleted:
            return [[p - bisect.bisect_left(self._deleted, p) for p in found] for found in result]
        return result

    def addCols(self, **args):
        """Add new columns
        Args:
            **args: column names and types
        """
        n = len(self.struct)
        self._commit([AddCol(name, t, n + k) for k, (name, t) in enumerate(args.items())])

    def editByName(self, col, where, change):
        """Edit cells by column name and value. Will edit multiple cells if they fit the condition.
        Args:
            col: column name
            where: value to match
            change: new value
        """
        self._commit([SetCell(i, col, change) for i in self.find(col, where)])

    def add(self, **args):
        """Add a row with keyword args as column=value. Unknown columns are ignored.
        Args:
            **args: column names and values
        Returns:
            None
        Example:
            `table.add(name="John Doe", address="123 Place St.")`
        """
        names = self.struct.column_names
        self._commit([AddRow(len(self.rows), {k: v for k, v in args.items() if k in names})])

