import io
import os
import uuid
from typing import Type, Self

try:
    from .table_file import TableDiff, DiffOp, AddCol, AddRow, SetCell, commit_line
except ImportError:
    from table_file import TableDiff, DiffOp, AddCol, AddRow, SetCell, commit_line

FILE_ENCODING = "utf-8"

class RowStruct():
    def __iter__(self):
        for i in range(len(self.column_names)):
            yield i

    def __len__(self):
        return len(self.columns)

    def __init__(self, **args):
        self.columns:list[Type] = [i for i in args.values()]
        self.column_names:list[str] = [i for i in args.keys()]

    def add(self, **args):
        self.columns.extend([i for i in args.values()])
        self.column_names.extend([i for i in args.keys()])

    def col(self, name: str | int) -> int:
        return self.column_names.index(name) if isinstance(name, str) else name

class Cell():
    """Mutable value holder shared between a table's rows and columns."""
    def __init__(self, value=None):
        self.value = value

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return repr(self.value)


class Row():
    def __init__(self, struct:RowStruct, content = None):
        self.struct = struct
        if content is None:
            content = [None] * len(struct)
        self.content:list[Cell] = [c if isinstance(c, Cell) else Cell(c) for c in content]

    def __str__(self):
        return f"{self.content}"

    def _fill(self):
        # Pad with empty cells for columns added to the struct after this row was created
        while len(self.content) < len(self.struct):
            self.content.append(Cell())

    def col(self, name: str | int) -> Cell:
        """Get cell by column"""
        try:
            i = self.struct.col(name)
        except ValueError:
            raise IndexError("Column not found")
        self._fill()
        return self.content[i]

    def append(self, item, nv):
        """Set cell by column name
        Args:
            item: column name
            nv: new value
        """
        try:
            self.col(item).value = nv
        except IndexError:
            return "Column not found"


class Table():
    rows:list[Row]
    cols:dict[str, list[Cell]]
    struct:RowStruct
    _file:str

    @property
    def entries(self) -> list[Row]:
        return self.rows

    def __init__(self, t:list[Row] | None = None, *, file=None):
        """
        Args:
            t: initial rows (only allowed when the file is new or empty)
            file: table file path. An existing file is loaded by replaying its logged changes.
        """
        self.rows = []
        self.struct = RowStruct()
        self.cols = {}
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
        return lambda: self._drop_col(name)

    def _drop_col(self, name:str):
        i = self.struct.col(name)
        for r in self.rows:
            r._fill()
        type = self.struct.columns.pop(i)
        self.struct.column_names.pop(i)
        removed = [r.content.pop(i) for r in self.rows]
        cells = self.cols.pop(name)

        def undo():
            self.struct.columns.insert(i, type)
            self.struct.column_names.insert(i, name)
            for r, c in zip(self.rows, removed):
                r.content.insert(i, c)
            self.cols[name] = cells
        return undo

    def _set_col_type(self, name:str, type:Type):
        i = self.struct.col(name)
        old = self.struct.columns[i]
        self.struct.columns[i] = type
        return lambda: self.struct.columns.__setitem__(i, old)

    def _insert_row(self, index:int, values:dict):
        r = Row(self.struct)
        for name, value in values.items():
            r.col(name).value = value
        n = len(self.rows)
        index = min(index if index >= 0 else max(0, n + index), n)  # same position list.insert would use
        self.rows.insert(index, r)
        for name in self.struct.column_names:
            self.cols[name].insert(index, r.col(name))
        return lambda: self._drop_row(index)

    def _drop_row(self, index:int):
        r = self.rows.pop(index)
        index = index % (len(self.rows) + 1)  # normalize negative indices for undo
        for cells in self.cols.values():
            cells.pop(index)

        def undo():
            self.rows.insert(index, r)
            for name in self.struct.column_names:
                self.cols[name].insert(index, r.col(name))
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
        width = [max(len(head[j]), *(len(r[j]) for r in body), 0) for j in range(len(names))]

        def rule(left, mid, right):
            return left + mid.join("─" * (w + 2) for w in width) + right

        def line(cells):
            return "│" + "│".join(f" {v:<{w}} " for v, w in zip(cells, width)) + "│"

        sep = rule("├", "┼", "┤")
        out = [rule("┌", "┬", "┐"), line(head), sep]
        for n, r in enumerate(body):
            if n:
                out.append(sep)
            out.append(line(r))
        out.append(rule("└", "┴", "┘"))
        return "\n".join(out)

    def select(self, var, col_name=None):
        """Select rows by value
        Args:
            var: value to match
            col_name: column to check
        """
        if col_name is None:
            t:Type = type(var)
            for i in self.struct:
                if self.struct.columns[i] == t:
                    for j in self.rows:
                        if var == j.col(i).value:
                            yield j
        else:
            for row, cell in zip(self.rows, self.column(col_name)):
                if var == cell.value:
                    yield row

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
        self._commit([SetCell(i, col, change) for i, cell in enumerate(self.column(col)) if cell.value == where])

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

if __name__ == "__main__":
    t = Table()
    t.addCols(name=str, address=str, phone=str)
    t.add(name="John Doe", address="123 Place St.", phone="(592) 010-2345")
    t.add(name="Jimmy Doe", address="123 Place St. (Basement)")
    t.addCols(email=str)
    t.editByName("email", None, "email@placeholder.org")

    for i in t.select("John Doe"):
        print(i)

    for i in t.select("Jimmy Doe", "name"):
        print(i)

    print(t)

