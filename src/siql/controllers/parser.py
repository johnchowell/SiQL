"""Tokenizer and parser for SiQL's SQL-like query language.

    CREATE TABLE [IF NOT EXISTS] name (col type, ...)
    DROP TABLE [IF EXISTS] name
    ALTER TABLE name ADD [COLUMN] col type | DROP [COLUMN] col | ALTER [COLUMN] col [TYPE] type
                   | RENAME [COLUMN] col TO new_col
    RENAME TABLE name TO new_name
    COPY TABLE name TO new_name
    TRUNCATE [TABLE] name
    INSERT INTO name [(col, ...)] VALUES (value, ...), ...
    SELECT * | col, ... | COUNT(*) FROM name [WHERE cond] [ORDER BY col [ASC|DESC], ...] [LIMIT n [OFFSET m]]
    UPDATE name SET col = value, ... [WHERE cond]
    DELETE FROM name [WHERE cond]
    SHOW TABLES | SHOW FILES
    DESCRIBE name

File management:
    USE 'folder'                     switch to another folder of table files (created if missing)
    COMPACT [TABLE] [name]           rewrite a table's file (or every table's) without its change history
    IMPORT 'file.csv' INTO name      add CSV rows to a table, creating it if needed
    EXPORT [TABLE] name TO 'file.csv'

Conditions combine `a = b`, `!=`/`<>`, `<`, `>`, `<=`, `>=`, `IS [NOT] NULL`, `[NOT] IN (...)` and
`[NOT] LIKE 'pattern'` (`%` any text, `_` one character, case-insensitive) with AND, OR, NOT and parentheses.
Values are 'strings' or "strings" (double the quote to escape it), numbers, TRUE, FALSE and NULL.
Keywords are case-insensitive; quote a column named like a keyword with backticks: `order`.
"""
import operator
import re
from dataclasses import dataclass, field
from typing import Any, NoReturn, Type


class QueryError(Exception):
    """Raised for a query that can't be parsed or run."""


OPERATORS = {
    "=": operator.eq, "==": operator.eq, "!=": operator.ne, "<>": operator.ne,
    "<": operator.lt, ">": operator.gt, "<=": operator.le, ">=": operator.ge,
}

COLUMN_TYPES: dict[str, Type] = {
    "str": str, "text": str, "string": str, "varchar": str,
    "int": int, "integer": int,
    "float": float, "real": float, "double": float,
    "bool": bool, "boolean": bool,
    "list": list, "dict": dict,
    "object": object, "any": object,
}

RESERVED = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "ORDER", "BY", "LIMIT", "OFFSET", "INSERT", "INTO",
    "VALUES", "UPDATE", "SET", "DELETE", "CREATE", "DROP", "ALTER", "TABLE", "IS", "NULL", "NONE", "IN",
    "LIKE", "ASC", "DESC", "TRUE", "FALSE", "SHOW", "DESCRIBE",
}

_TOKEN = re.compile(r"""
      (?P<ws>\s+|--[^\n]*)
    | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
    | (?P<qname>`(?:[^`]|``)+`)
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op><=|>=|!=|<>|==|[=<>(),*;-])
""", re.VERBOSE)


@dataclass(frozen=True)
class Token:
    kind: str  # "name", "qname" (backtick-quoted name), "str", "num", "op" or "end"
    value: Any
    pos: int


def tokenize(text: str) -> list[Token]:
    tokens = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise QueryError(f"Unexpected character {text[pos]!r} at position {pos}")
        kind, raw = m.lastgroup, m.group()
        if kind == "num":
            tokens.append(Token(kind, float(raw) if any(c in raw for c in ".eE") else int(raw), pos))
        elif kind == "str":
            tokens.append(Token(kind, raw[1:-1].replace(raw[0] * 2, raw[0]), pos))
        elif kind == "qname":
            tokens.append(Token(kind, raw[1:-1].replace("``", "`"), pos))
        elif kind != "ws":
            tokens.append(Token(kind, raw, pos))
        pos = m.end()
    tokens.append(Token("end", None, pos))
    return tokens


# Condition expressions. `eval` takes a row as {column: value}.
class Expr():
    def eval(self, row: dict) -> Any:
        raise NotImplementedError

    def columns(self) -> set[str]:
        return set()


@dataclass
class Column(Expr):
    name: str

    def eval(self, row):
        return row[self.name]

    def columns(self):
        return {self.name}


@dataclass
class Literal(Expr):
    value: Any

    def eval(self, row):
        return self.value


@dataclass
class Compare(Expr):
    left: Expr
    op: str
    right: Expr

    def eval(self, row):
        try:
            return bool(OPERATORS[self.op](self.left.eval(row), self.right.eval(row)))
        except TypeError:  # e.g. None < 1 or "a" > 2
            return False

    def columns(self):
        return self.left.columns() | self.right.columns()


@dataclass
class And(Expr):
    items: list[Expr]

    def eval(self, row):
        return all(i.eval(row) for i in self.items)

    def columns(self):
        return set().union(*(i.columns() for i in self.items))


@dataclass
class Or(Expr):
    items: list[Expr]

    def eval(self, row):
        return any(i.eval(row) for i in self.items)

    def columns(self):
        return set().union(*(i.columns() for i in self.items))


@dataclass
class Not(Expr):
    item: Expr

    def eval(self, row):
        return not self.item.eval(row)

    def columns(self):
        return self.item.columns()


@dataclass
class IsNull(Expr):
    operand: Expr
    negate: bool = False

    def eval(self, row):
        return (self.operand.eval(row) is None) != self.negate

    def columns(self):
        return self.operand.columns()


@dataclass
class In(Expr):
    operand: Expr
    values: list[Expr]
    negate: bool = False

    def eval(self, row):
        v = self.operand.eval(row)
        return any(v == x.eval(row) for x in self.values) != self.negate

    def columns(self):
        return self.operand.columns().union(*(x.columns() for x in self.values))


@dataclass
class Like(Expr):
    operand: Expr
    pattern: str
    negate: bool = False
    _regex: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        parts = (".*" if c == "%" else "." if c == "_" else re.escape(c) for c in self.pattern)
        self._regex = re.compile("".join(parts), re.IGNORECASE | re.DOTALL)

    def eval(self, row):
        v = self.operand.eval(row)
        return isinstance(v, str) and bool(self._regex.fullmatch(v)) != self.negate

    def columns(self):
        return self.operand.columns()


# Statements
@dataclass
class CreateTable:
    table: str
    columns: list[tuple[str, Type]]
    if_not_exists: bool = False


@dataclass
class DropTable:
    table: str
    if_exists: bool = False


@dataclass
class AddColumn:
    table: str
    column: str
    type: Type


@dataclass
class DropColumn:
    table: str
    column: str


@dataclass
class AlterColumn:
    table: str
    column: str
    type: Type


@dataclass
class Insert:
    table: str
    columns: list[str] | None
    rows: list[list[Any]]


@dataclass
class Select:
    table: str
    columns: list[str] | None  # None means *
    count: bool = False
    where: Expr | None = None
    order: list[tuple[str, bool]] = field(default_factory=list)  # (column, descending)
    limit: int | None = None
    offset: int = 0


@dataclass
class Update:
    table: str
    values: dict[str, Any]
    where: Expr | None = None


@dataclass
class Delete:
    table: str
    where: Expr | None = None


@dataclass
class ShowTables:
    pass


@dataclass
class Describe:
    table: str


@dataclass
class RenameColumn:
    table: str
    column: str
    new_name: str


@dataclass
class RenameTable:
    table: str
    new_name: str


@dataclass
class CopyTable:
    table: str
    new_name: str


@dataclass
class Truncate:
    table: str


@dataclass
class Compact:
    table: str | None  # None means every table


@dataclass
class Use:
    path: str


@dataclass
class ShowFiles:
    pass


@dataclass
class Import:
    path: str
    table: str


@dataclass
class Export:
    table: str
    path: str


Statement = (CreateTable | DropTable | AddColumn | DropColumn | AlterColumn | Insert | Select | Update
             | Delete | ShowTables | Describe | RenameColumn | RenameTable | CopyTable | Truncate | Compact
             | Use | ShowFiles | Import | Export)


def parse(text: str) -> list[Statement]:
    """Parse one or more `;`-separated statements."""
    return Parser(text).script()


class Parser():
    def __init__(self, text: str):
        self.tokens = tokenize(text)
        self.i = 0

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        t = self.tok
        if t.kind != "end":
            self.i += 1
        return t

    def error(self, message: str) -> NoReturn:
        t = self.tok
        got = "end of query" if t.kind == "end" else repr(t.value)
        raise QueryError(f"{message} at position {t.pos}, got {got}")

    def is_kw(self, *words: str) -> bool:
        return self.tok.kind == "name" and self.tok.value.upper() in words

    def accept_kw(self, *words: str) -> str | None:
        return self.advance().value.upper() if self.is_kw(*words) else None

    def expect_kw(self, *words: str) -> str:
        return self.accept_kw(*words) or self.error(f"Expected {' or '.join(words)}")

    def accept_op(self, *ops: str) -> str | None:
        return self.advance().value if self.tok.kind == "op" and self.tok.value in ops else None

    def expect_op(self, *ops: str) -> str:
        return self.accept_op(*ops) or self.error(f"Expected {' or '.join(repr(o) for o in ops)}")

    def ident(self, what: str = "a name") -> str:
        t = self.tok
        if t.kind == "qname" or (t.kind == "name" and t.value.upper() not in RESERVED):
            return self.advance().value
        self.error(f"Expected {what}")

    def idents(self, what: str) -> list[str]:
        names = [self.ident(what)]
        while self.accept_op(","):
            names.append(self.ident(what))
        return names

    def column_type(self) -> Type:
        t = self.tok
        if t.kind == "name" and t.value.lower() in COLUMN_TYPES:
            return COLUMN_TYPES[self.advance().value.lower()]
        self.error(f"Expected a column type ({', '.join(COLUMN_TYPES)})")

    def literal(self) -> Any:
        t = self.tok
        if t.kind in ("str", "num"):
            return self.advance().value
        if self.accept_op("-"):
            if self.tok.kind != "num":
                self.error("Expected a number")
            return -self.advance().value
        word = self.accept_kw("NULL", "NONE", "TRUE", "FALSE")
        if word:
            return {"NULL": None, "NONE": None, "TRUE": True, "FALSE": False}[word]
        self.error("Expected a value")

    def whole_number(self) -> int:
        if self.tok.kind != "num" or not isinstance(self.tok.value, int):
            self.error("Expected a whole number")
        return self.advance().value

    def script(self) -> list[Statement]:
        statements = []
        while self.tok.kind != "end":
            if self.accept_op(";"):
                continue
            statements.append(self.statement())
            if self.tok.kind != "end":
                self.expect_op(";")
        return statements

    def statement(self) -> Statement:
        kw = self.expect_kw("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "SHOW", "DESCRIBE",
                            "RENAME", "COPY", "TRUNCATE", "COMPACT", "USE", "IMPORT", "EXPORT")
        return getattr(self, f"_{kw.lower()}")()

    def path(self) -> str:
        if self.tok.kind != "str":
            self.error("Expected a quoted file path")
        return self.advance().value

    def _rename(self):
        self.expect_kw("TABLE")
        table = self.ident("a table name")
        self.expect_kw("TO")
        return RenameTable(table, self.ident("a table name"))

    def _copy(self):
        self.expect_kw("TABLE")
        table = self.ident("a table name")
        self.expect_kw("TO")
        return CopyTable(table, self.ident("a table name"))

    def _truncate(self):
        self.accept_kw("TABLE")
        return Truncate(self.ident("a table name"))

    def _compact(self):
        if self.accept_kw("TABLE") or self.tok.kind in ("name", "qname"):
            return Compact(self.ident("a table name"))
        return Compact(None)

    def _use(self):
        return Use(self.path())

    def _import(self):
        path = self.path()
        self.expect_kw("INTO")
        return Import(path, self.ident("a table name"))

    def _export(self):
        self.accept_kw("TABLE")
        table = self.ident("a table name")
        self.expect_kw("TO")
        return Export(table, self.path())

    def _create(self):
        self.expect_kw("TABLE")
        if_not_exists = bool(self.accept_kw("IF")) and bool(self.expect_kw("NOT")) and bool(self.expect_kw("EXISTS"))
        table = self.ident("a table name")
        self.expect_op("(")
        columns = []
        while True:
            name = self.ident("a column name")
            if any(name == c for c, _ in columns):
                raise QueryError(f"Duplicate column: {name}")
            columns.append((name, self.column_type()))
            if not self.accept_op(","):
                break
        self.expect_op(")")
        return CreateTable(table, columns, if_not_exists)

    def _drop(self):
        self.expect_kw("TABLE")
        if_exists = bool(self.accept_kw("IF")) and bool(self.expect_kw("EXISTS"))
        return DropTable(self.ident("a table name"), if_exists)

    def _alter(self):
        self.expect_kw("TABLE")
        table = self.ident("a table name")
        action = self.expect_kw("ADD", "DROP", "ALTER", "RENAME")
        self.accept_kw("COLUMN")
        column = self.ident("a column name")
        if action == "ADD":
            return AddColumn(table, column, self.column_type())
        if action == "DROP":
            return DropColumn(table, column)
        if action == "RENAME":
            self.expect_kw("TO")
            return RenameColumn(table, column, self.ident("a column name"))
        self.accept_kw("TYPE")
        return AlterColumn(table, column, self.column_type())

    def _insert(self):
        self.expect_kw("INTO")
        table = self.ident("a table name")
        columns = None
        if self.accept_op("("):
            columns = self.idents("a column name")
            self.expect_op(")")
        self.expect_kw("VALUES")
        rows = []
        while True:
            self.expect_op("(")
            row = [self.literal()]
            while self.accept_op(","):
                row.append(self.literal())
            self.expect_op(")")
            rows.append(row)
            if not self.accept_op(","):
                break
        return Insert(table, columns, rows)

    def _select(self):
        count, columns = False, None
        nxt = self.tokens[min(self.i + 1, len(self.tokens) - 1)]
        if self.is_kw("COUNT") and nxt.kind == "op" and nxt.value == "(":
            self.advance(), self.advance()
            self.expect_op("*")
            self.expect_op(")")
            count = True
        elif not self.accept_op("*"):
            columns = self.idents("a column name")
        self.expect_kw("FROM")
        s = Select(self.ident("a table name"), columns, count, self.where())
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            while True:
                s.order.append((self.ident("a column name"), self.accept_kw("ASC", "DESC") == "DESC"))
                if not self.accept_op(","):
                    break
        if self.accept_kw("LIMIT"):
            s.limit = self.whole_number()
            if self.accept_kw("OFFSET"):
                s.offset = self.whole_number()
        return s

    def _update(self):
        table = self.ident("a table name")
        self.expect_kw("SET")
        values = {}
        while True:
            column = self.ident("a column name")
            self.expect_op("=", "==")
            values[column] = self.literal()
            if not self.accept_op(","):
                break
        return Update(table, values, self.where())

    def _delete(self):
        self.expect_kw("FROM")
        return Delete(self.ident("a table name"), self.where())

    def _show(self):
        return ShowFiles() if self.expect_kw("TABLES", "FILES") == "FILES" else ShowTables()

    def _describe(self):
        return Describe(self.ident("a table name"))

    def where(self) -> Expr | None:
        return self.expr() if self.accept_kw("WHERE") else None

    def expr(self) -> Expr:
        items = [self.and_expr()]
        while self.accept_kw("OR"):
            items.append(self.and_expr())
        return items[0] if len(items) == 1 else Or(items)

    def and_expr(self) -> Expr:
        items = [self.not_expr()]
        while self.accept_kw("AND"):
            items.append(self.not_expr())
        return items[0] if len(items) == 1 else And(items)

    def not_expr(self) -> Expr:
        if self.accept_kw("NOT"):
            return Not(self.not_expr())
        return self.predicate()

    def predicate(self) -> Expr:
        if self.accept_op("("):
            e = self.expr()
            self.expect_op(")")
            return e
        left = self.operand()
        if self.accept_kw("IS"):
            negate = bool(self.accept_kw("NOT"))
            self.expect_kw("NULL", "NONE")
            return IsNull(left, negate)
        negate = bool(self.accept_kw("NOT"))
        if self.accept_kw("IN"):
            self.expect_op("(")
            values = [self.operand()]
            while self.accept_op(","):
                values.append(self.operand())
            self.expect_op(")")
            return In(left, values, negate)
        if self.accept_kw("LIKE"):
            if self.tok.kind != "str":
                self.error("Expected a string pattern")
            return Like(left, self.advance().value, negate)
        if negate:
            self.error("Expected IN or LIKE")
        op = self.expect_op(*OPERATORS)
        return Compare(left, op, self.operand())

    def operand(self) -> Expr:
        t = self.tok
        if t.kind == "qname" or (t.kind == "name" and t.value.upper() not in RESERVED):
            return Column(self.advance().value)
        return Literal(self.literal())
