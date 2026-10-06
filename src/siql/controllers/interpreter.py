import ast
import csv
import os
from dataclasses import dataclass, field
from typing import Any, Type

from ..models.table import Table
from ..models.diff import TableDiff, AddCol, DropCol, SetColType, RenameCol, AddRow, DropRow, SetCell
from ..managers.database import Database
from ..helpers.format import box
from ..helpers.types import type_name
from .parser import (QueryError, OPERATORS, parse, Statement, Expr, Column, Literal, Compare, And,
                     CreateTable, DropTable, AddColumn,
                     DropColumn, AlterColumn, Insert, Select, Update, Delete, ShowTables, Describe,
                     RenameColumn, RenameTable, CopyTable, Truncate, Compact, Use, ShowFiles, Import, Export)


class Command():
    SELECT = 0
    WHERE = 1
    FROM = 2
    AND = 3
    OR = 4
    ORDER = 5
    BY = 6

    def strToOp(s:str):
        """Comparison function for an operator: <, >, <=, >=, =, ==, != or <>."""
        try:
            return OPERATORS[s]
        except KeyError:
            raise ValueError(f"Unknown operator: {s}") from None

    def fromStr(s:str) -> int:
        match s.lower().strip():
            case "select":
                return 0
            case "where":
                return 1
            case "equals":
                return 2
            case "from":
                return 3
            case "and":
                return 4
            case "or":
                return 5
            case "if":
                return 6


@dataclass
class Result():
    """Outcome of one statement. `rowcount` is the number of rows returned (SELECT) or changed."""
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    rowcount: int = 0
    message: str = ""

    def __iter__(self):
        return iter(self.rows)

    def to_dict(self) -> dict:
        return {"columns": self.columns, "rows": self.rows, "rowcount": self.rowcount, "message": self.message}

    def __str__(self):
        if self.columns:
            return box(self.columns, [[str(v) for v in r] for r in self.rows])
        return self.message


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _coerce(value, col_type: Type, column: str):
    """Check that `value` fits a column's type. Ints are accepted (as floats) in float columns."""
    if value is None or col_type is object:
        return value
    if col_type is float and type(value) is int:
        return float(value)
    if isinstance(value, col_type) and (type(value) is not bool or col_type is bool):
        return value
    raise QueryError(f"Column {column} holds {type_name(col_type)}, got {type_name(type(value))}: {value!r}")


_BOOLS = {"true": True, "false": False, "1": True, "0": False}


def _from_text(text: str, col_type: Type):
    """Read a CSV field as a column type. Empty fields are NULL. Raises ValueError."""
    if text == "":
        return None
    if col_type is str:
        return text
    if col_type is bool:
        key = text.strip().lower()
        if key not in _BOOLS:
            raise ValueError(text)
        return _BOOLS[key]
    if col_type in (int, float):
        return col_type(text)
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        if col_type is object:
            return text
        raise ValueError(text) from None
    if col_type is not object and not isinstance(value, col_type):
        raise ValueError(text)
    return value


def _to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else repr(value)


def _infer_type(texts: list[str]) -> Type:
    """Narrowest of int, float, bool or str that can read every non-empty field."""
    texts = [t for t in texts if t != ""]
    for col_type in (int, float, bool):
        try:
            for t in texts:
                _from_text(t, col_type)
        except ValueError:
            continue
        if texts:
            return col_type
    return str


class Interpreter():
    """Runs SiQL queries against the tables of a Database (a folder of .siql files).

        db = Interpreter("data")
        db.execute("CREATE TABLE people (name str, age int)")
        db.execute("INSERT INTO people VALUES ('Ann', 31), ('Bob', 25)")
        db.execute("SELECT name FROM people WHERE age > 30").rows  # [['Ann']]

    Each statement is saved all-or-nothing. A script's statements are saved one at a time.
    With `file_access=False`, statements that touch files outside the data folder (USE, IMPORT, EXPORT)
    are refused; the server uses this so clients can't read or write arbitrary files.
    """
    def __init__(self, database: Database | str = ".", *, file_access: bool = True):
        self.database = database if isinstance(database, Database) else Database(database)
        self.file_access = file_access

    def execute(self, query: str) -> Result:
        """Run a single statement."""
        statements = parse(query)
        if len(statements) != 1:
            raise QueryError(f"execute() runs one statement, got {len(statements)}; use executescript()")
        return self.run(statements[0])

    def executescript(self, script: str) -> list[Result]:
        """Run `;`-separated statements. The whole script is parsed before any statement runs."""
        return [self.run(s) for s in parse(script)]

    def run(self, statement: Statement) -> Result:
        """Run a parsed statement."""
        handlers = {
            CreateTable: self._create, DropTable: self._drop, AddColumn: self._add_column,
            DropColumn: self._drop_column, AlterColumn: self._alter_column, Insert: self._insert,
            Select: self._select, Update: self._update, Delete: self._delete,
            ShowTables: self._show, Describe: self._describe, RenameColumn: self._rename_column,
            RenameTable: self._rename_table, CopyTable: self._copy_table, Truncate: self._truncate,
            Compact: self._compact, Use: self._use, ShowFiles: self._show_files, Import: self._import,
            Export: self._export,
        }
        return handlers[type(statement)](statement)

    def _check_file_access(self, statement: str):
        if not self.file_access:
            raise QueryError(f"{statement} is disabled here because it accesses files outside the data folder")

    def _table(self, name: str) -> Table:
        try:
            return self.database.table(name)
        except (KeyError, ValueError) as e:
            raise QueryError(e.args[0]) from None

    @staticmethod
    def _types(table: Table) -> dict[str, Type]:
        return dict(zip(table.struct.column_names, table.struct.columns))

    @staticmethod
    def _check_columns(table: Table, columns) -> None:
        missing = [c for c in columns if c not in table.struct.column_names]
        if missing:
            raise QueryError(f"No such column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}")

    @staticmethod
    def _record(table: Table, i: int) -> dict[str, Any]:
        row = table.rows[i]
        return {n: row.col(j).value for j, n in enumerate(table.struct.column_names)}

    @staticmethod
    def _candidates(table: Table, where: Expr) -> list[int] | None:
        """Rows passing a `column = value` test that the whole condition requires, found with the table's
        search tree. None if there's no such test or the tree is off."""
        if not table.tree:
            return None
        for e in (where.items if isinstance(where, And) else [where]):
            if isinstance(e, Compare) and OPERATORS[e.op] is OPERATORS["="]:
                column, literal = (e.left, e.right) if isinstance(e.left, Column) else (e.right, e.left)
                if isinstance(column, Column) and isinstance(literal, Literal):
                    return table.find(column.name, literal.value)
        return None

    def _matching(self, table: Table, where: Expr | None) -> list[int]:
        if where is None:
            return list(range(len(table.rows)))
        self._check_columns(table, where.columns())
        candidates = self._candidates(table, where)
        indices = range(len(table.rows)) if candidates is None else candidates
        return [i for i in indices if where.eval(self._record(table, i))]

    def _create(self, s: CreateTable) -> Result:
        if s.table in self.database:
            if s.if_not_exists:
                return Result(message=f"Table {s.table} already exists")
            raise QueryError(f"Table already exists: {s.table}")
        try:
            self.database.create(s.table, **dict(s.columns))
        except ValueError as e:
            raise QueryError(e.args[0]) from None
        return Result(message=f"Created table {s.table}")

    def _drop(self, s: DropTable) -> Result:
        if s.if_exists and s.table not in self.database:
            return Result(message=f"Table {s.table} doesn't exist")
        try:
            self.database.drop(s.table)
        except (KeyError, ValueError) as e:
            raise QueryError(e.args[0]) from None
        return Result(message=f"Dropped table {s.table}")

    def _add_column(self, s: AddColumn) -> Result:
        t = self._table(s.table)
        if s.column in t.struct.column_names:
            raise QueryError(f"Column already exists: {s.column}")
        TableDiff([AddCol(s.column, s.type, len(t.struct))]).apply(t)
        return Result(message=f"Added column {s.column}")

    def _drop_column(self, s: DropColumn) -> Result:
        t = self._table(s.table)
        self._check_columns(t, [s.column])
        TableDiff([DropCol(s.column)]).apply(t)
        return Result(message=f"Dropped column {s.column}")

    def _alter_column(self, s: AlterColumn) -> Result:
        t = self._table(s.table)
        self._check_columns(t, [s.column])
        for cell in t.column(s.column):
            _coerce(cell.value, s.type, s.column)
        TableDiff([SetColType(s.column, s.type)]).apply(t)
        return Result(message=f"Changed column {s.column} to {type_name(s.type)}")

    def _insert(self, s: Insert) -> Result:
        t = self._table(s.table)
        columns = s.columns or list(t.struct.column_names)
        self._check_columns(t, columns)
        if len(set(columns)) != len(columns):
            raise QueryError("A column is listed more than once")
        types = self._types(t)
        ops = []
        for row in s.rows:
            if len(row) != len(columns):
                raise QueryError(f"Got {len(row)} values for {len(columns)} columns")
            ops.append(AddRow(len(t.rows) + len(ops), {c: _coerce(v, types[c], c) for c, v in zip(columns, row)}))
        TableDiff(ops).apply(t)
        return Result(rowcount=len(ops), message=f"Inserted {_plural(len(ops), 'row')}")

    def _select(self, s: Select) -> Result:
        t = self._table(s.table)
        self._check_columns(t, (s.columns or []) + [c for c, _ in s.order])
        rows = [self._record(t, i) for i in self._matching(t, s.where)]
        # Stable sorts from the last key to the first give a multi-column ORDER BY; NULLs sort first
        for column, descending in reversed(s.order):
            try:
                rows.sort(key=lambda r: (r[column] is not None, r[column]), reverse=descending)
            except TypeError:
                raise QueryError(f"Can't order by {column}: it holds values of different types") from None
        rows = rows[s.offset:None if s.limit is None else s.offset + s.limit]
        if s.count:
            return Result(["count"], [[len(rows)]], 1, _plural(len(rows), "row"))
        columns = s.columns or list(t.struct.column_names)
        return Result(columns, [[r[c] for c in columns] for r in rows], len(rows), _plural(len(rows), "row"))

    def _update(self, s: Update) -> Result:
        t = self._table(s.table)
        self._check_columns(t, s.values)
        types = self._types(t)
        values = {c: _coerce(v, types[c], c) for c, v in s.values.items()}
        matched = self._matching(t, s.where)
        TableDiff([SetCell(i, c, v) for i in matched for c, v in values.items()]).apply(t)
        return Result(rowcount=len(matched), message=f"Updated {_plural(len(matched), 'row')}")

    def _delete(self, s: Delete) -> Result:
        t = self._table(s.table)
        matched = self._matching(t, s.where)
        TableDiff([DropRow(i) for i in reversed(matched)]).apply(t)
        return Result(rowcount=len(matched), message=f"Deleted {_plural(len(matched), 'row')}")

    def _show(self, s: ShowTables) -> Result:
        tables = self.database.tables()
        return Result(["table"], [[n] for n in tables], len(tables), _plural(len(tables), "table"))

    def _describe(self, s: Describe) -> Result:
        t = self._table(s.table)
        rows = [[n, type_name(ty)] for n, ty in self._types(t).items()]
        return Result(["column", "type"], rows, len(rows), _plural(len(rows), "column"))

    def _rename_column(self, s: RenameColumn) -> Result:
        t = self._table(s.table)
        self._check_columns(t, [s.column])
        if s.new_name in t.struct.column_names:
            raise QueryError(f"Column already exists: {s.new_name}")
        TableDiff([RenameCol(s.column, s.new_name)]).apply(t)
        return Result(message=f"Renamed column {s.column} to {s.new_name}")

    def _rename_table(self, s: RenameTable) -> Result:
        try:
            self.database.rename(s.table, s.new_name)
        except (KeyError, ValueError) as e:
            raise QueryError(e.args[0]) from None
        return Result(message=f"Renamed table {s.table} to {s.new_name}")

    def _copy_table(self, s: CopyTable) -> Result:
        try:
            self.database.copy(s.table, s.new_name)
        except (KeyError, ValueError) as e:
            raise QueryError(e.args[0]) from None
        return Result(message=f"Copied table {s.table} to {s.new_name}")

    def _truncate(self, s: Truncate) -> Result:
        t = self._table(s.table)
        n = len(t.rows)
        TableDiff([DropRow(i) for i in reversed(range(n))]).apply(t)
        return Result(rowcount=n, message=f"Deleted {_plural(n, 'row')}")

    def _compact(self, s: Compact) -> Result:
        rows = []
        for name in [s.table] if s.table else self.database.tables():
            t = self._table(name)
            before = os.path.getsize(self.database.file(name))
            t.compact()
            rows.append([name, before, os.path.getsize(self.database.file(name))])
        return Result(["table", "bytes_before", "bytes_after"], rows, len(rows), f"Compacted {_plural(len(rows), 'table')}")

    def _use(self, s: Use) -> Result:
        self._check_file_access("USE")
        try:
            self.database = Database(s.path)
        except OSError as e:
            raise QueryError(f"Can't use {s.path}: {e.strerror}") from None
        return Result(message=f"Using {self.database.path}")

    def _show_files(self, s: ShowFiles) -> Result:
        rows = []
        for name in self.database.tables():
            file = self.database.file(name)
            rows.append([name, file, os.path.getsize(file), len(self._table(name).rows)])
        return Result(["table", "file", "bytes", "rows"], rows, len(rows), _plural(len(rows), "table"))

    def _import(self, s: Import) -> Result:
        self._check_file_access("IMPORT")
        try:
            with open(s.path, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                lines = list(reader)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            raise QueryError(f"Can't read {s.path}: {e}") from None
        if not header or any(not h for h in header) or len(set(header)) != len(header):
            raise QueryError(f"{s.path} needs a header row of unique column names")
        for n, line in enumerate(lines, start=2):
            if len(line) != len(header):
                raise QueryError(f"{s.path} line {n} has {len(line)} fields, expected {len(header)}")

        exists = s.table in self.database
        if exists:
            t = self._table(s.table)
            self._check_columns(t, header)
            types = self._types(t)
        else:
            types = {h: _infer_type([line[i] for line in lines]) for i, h in enumerate(header)}
        records = []
        for n, line in enumerate(lines, start=2):
            record = {}
            for h, text in zip(header, line):
                try:
                    record[h] = _from_text(text, types[h])
                except ValueError:
                    raise QueryError(f"{s.path} line {n}: can't read {text!r} as {type_name(types[h])} "
                                     f"for column {h}") from None
            records.append(record)

        if not exists:
            try:
                t = self.database.create(s.table, **types)
            except ValueError as e:
                raise QueryError(e.args[0]) from None
        TableDiff([AddRow(len(t.rows) + i, r) for i, r in enumerate(records)]).apply(t)
        return Result(rowcount=len(records), message=f"Imported {_plural(len(records), 'row')} into {s.table}")

    def _export(self, s: Export) -> Result:
        self._check_file_access("EXPORT")
        t = self._table(s.table)
        names = t.struct.column_names
        try:
            with open(s.path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(names)
                writer.writerows([_to_text(r.col(i).value) for i in range(len(names))] for r in t.rows)
        except OSError as e:
            raise QueryError(f"Can't write {s.path}: {e.strerror}") from None
        return Result(rowcount=len(t.rows), message=f"Exported {_plural(len(t.rows), 'row')} to {s.path}")
