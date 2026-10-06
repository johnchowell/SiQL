
import ast
import builtins
from difflib import SequenceMatcher
from typing import Any, Type, TextIO, Self, TYPE_CHECKING

# table.py imports this module, so Table is only imported lazily / for type checking here
if TYPE_CHECKING:
    from table import Table


def _type_name(t: Type) -> str:
    return t.__name__

def _type_from_name(name: str) -> Type:
    t = getattr(builtins, name, None)
    if isinstance(t, type):
        return t
    return type(None) if name == "NoneType" else object


class DiffOp():
    """A single change in a TableDiff. Row indices refer to the table state at the time the op is applied."""
    code: str = ""

    def args(self) -> tuple:
        return ()

    def apply(self, table: "Table"):
        raise NotImplementedError

    def to_tuple(self) -> tuple:
        return (self.code, *self.args())

    def dumps(self) -> str:
        """One line of the table file. Raises TypeError if a value can't be read back."""
        line = repr(self.to_tuple())
        try:
            ast.literal_eval(line)
        except (ValueError, SyntaxError):
            raise TypeError(f"{self!r} contains a value that can't be written to a table file") from None
        return line + "\n"

    @classmethod
    def from_args(cls, *args) -> Self:
        return cls(*args)

    def __eq__(self, other):
        return type(self) is type(other) and self.args() == other.args()

    def __repr__(self):
        return f"{type(self).__name__}{self.args()!r}"


class AddCol(DiffOp):
    code = "+c"

    def __init__(self, name: str, type: Type, index: int):
        self.name, self.type, self.index = name, type, index

    def args(self):
        return (self.name, _type_name(self.type), self.index)

    @classmethod
    def from_args(cls, name, type_name, index):
        return cls(name, _type_from_name(type_name), index)

    def apply(self, table: "Table"):
        table._insert_col(self.name, self.type, self.index)


class DropCol(DiffOp):
    code = "-c"

    def __init__(self, name: str):
        self.name = name

    def args(self):
        return (self.name,)

    def apply(self, table: "Table"):
        table._drop_col(self.name)


class SetColType(DiffOp):
    code = "=c"

    def __init__(self, name: str, type: Type):
        self.name, self.type = name, type

    def args(self):
        return (self.name, _type_name(self.type))

    @classmethod
    def from_args(cls, name, type_name):
        return cls(name, _type_from_name(type_name))

    def apply(self, table: "Table"):
        table._set_col_type(self.name, self.type)


class AddRow(DiffOp):
    code = "+r"

    def __init__(self, index: int, values: dict[str, Any]):
        self.index, self.values = index, values

    def args(self):
        return (self.index, self.values)

    def apply(self, table: "Table"):
        table._insert_row(self.index, self.values)


class DropRow(DiffOp):
    code = "-r"

    def __init__(self, index: int):
        self.index = index

    def args(self):
        return (self.index,)

    def apply(self, table: "Table"):
        table._drop_row(self.index)


class SetCell(DiffOp):
    code = "~"

    def __init__(self, row: int, col: str, value: Any):
        self.row, self.col, self.value = row, col, value

    def args(self):
        return (self.row, self.col, self.value)

    def apply(self, table: "Table"):
        table._set_cell(self.row, self.col, self.value)


_OPS: dict[str, type[DiffOp]] = {op.code: op for op in (AddCol, DropCol, SetColType, AddRow, DropRow, SetCell)}


class TableDiff():
    """Ordered list of DiffOps that turns one table into another.

    Text form is one op per line, each a Python literal tuple, e.g.
        ('+c', 'email', 'str', 3)
        ('~', 1, 'email', 'email@placeholder.org')
    so it can be appended to a table file and replayed with `loads` + `apply`.
    Cell values must be Python literals (str, int, float, bool, None, list, dict, ...) to round-trip.
    """

    def __init__(self, ops: list[DiffOp] | None = None):
        self.ops: list[DiffOp] = list(ops) if ops else []

    def __iter__(self):
        return iter(self.ops)

    def __len__(self):
        return len(self.ops)

    def __bool__(self):
        return bool(self.ops)

    def __eq__(self, other):
        return isinstance(other, TableDiff) and self.ops == other.ops

    def __repr__(self):
        return f"TableDiff({self.ops!r})"

    def __str__(self):
        return self.dumps()

    @classmethod
    def between(cls, old: "Table", new: "Table") -> Self:
        """Build the diff that transforms `old` into `new`."""
        ops: list[DiffOp] = []
        old_types = dict(zip(old.struct.column_names, old.struct.columns))
        new_names = list(new.struct.column_names)
        new_types = dict(zip(new_names, new.struct.columns))

        # Columns first, so row ops below can address every column in `new`
        for name in old.struct.column_names:
            if name not in new_types:
                ops.append(DropCol(name))
        for i, name in enumerate(new_names):
            if name not in old_types:
                ops.append(AddCol(name, new_types[name], i))
            elif old_types[name] is not new_types[name]:
                ops.append(SetColType(name, new_types[name]))

        def values(table: "Table") -> list[tuple]:
            present = set(table.struct.column_names)
            return [tuple(r.col(n).value if n in present else None for n in new_names) for r in table.rows]

        old_vals, new_vals = values(old), values(new)
        # repr keys keep SequenceMatcher working with unhashable cell values
        matcher = SequenceMatcher(None, [repr(v) for v in old_vals], [repr(v) for v in new_vals], autojunk=False)

        offset = 0  # shift between `old` indices and the partially-patched table
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            pos = i1 + offset
            paired = min(i2 - i1, j2 - j1) if tag == "replace" else 0
            for k in range(paired):
                for name, o, n in zip(new_names, old_vals[i1 + k], new_vals[j1 + k]):
                    if not o == n:
                        ops.append(SetCell(pos + k, name, n))
            for _ in range(i2 - i1 - paired):
                ops.append(DropRow(pos + paired))
            for k in range(j2 - j1 - paired):
                ops.append(AddRow(pos + paired + k, dict(zip(new_names, new_vals[j1 + paired + k]))))
            offset += (j2 - j1) - (i2 - i1)

        return cls(ops)

    def apply(self, table: "Table") -> "Table":
        """Apply ops in order to `table` (in place) and return it. The changes are logged to the table's file."""
        table._commit(self.ops)
        return table

    def dumps(self) -> str:
        return "".join(op.dumps() for op in self.ops)

    @classmethod
    def loads(cls, text: str) -> Self:
        ops = []
        for line in text.splitlines():
            if not line.strip():
                continue
            code, *args = ast.literal_eval(line)
            ops.append(_OPS[code].from_args(*args))
        return cls(ops)

    def write(self, fp: TextIO):
        """Write the text form to an open file or buffer (e.g. `Table._buffer`)."""
        fp.write(self.dumps())


class table_file:
    table:"Table"
    def __init__(self):
        try:
            from .table import Table
        except ImportError:
            from table import Table
        self.table = Table()

    def diff(self, table:"Table") -> TableDiff:
        """Return the changes needed to turn this file's table into `table`.
        Apply them with `diff.apply(self.table)` (which also logs them to its file) or write them with `diff.write(...)`.
        """
        return TableDiff.between(self.table, table)
