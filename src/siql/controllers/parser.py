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
    SELECT ... FROM name [WHERE cond] ORDER BY COSINE(col, [x, ...]) | DOT(col, [x, ...]) [DESC|ASC] [LIMIT n ...]
    SELECT [DISTINCT] item [[AS] name], ... FROM source [[AS] alias]
           [[INNER | LEFT [OUTER] | CROSS] JOIN source [[AS] alias] [ON cond]] ...
           [WHERE cond] [GROUP BY expr, ...] [HAVING cond] [ORDER BY expr [ASC|DESC], ...] [LIMIT n [OFFSET m]]
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
A vector value (for a `vector` column) is a list of numbers in brackets: [0.1, -2, 3.5e-3].
COSINE and DOT order rows by their similarity to a query vector; they must be the only ORDER BY key.
Keywords are case-insensitive; quote a column named like a keyword with backticks: `order`.

In a SELECT, a source is a table or a `(SELECT ...)` with an alias, and `alias.col` names a column of one source.
An item is `*`, `alias.*` or an expression: columns, values, `+ - * /` (NULL in, NULL out; `/ 0` is NULL),
COALESCE, IFNULL, LOWER, UPPER, LENGTH, ABS, ROUND, COSINE, DOT, the aggregates COUNT(*), COUNT([DISTINCT] e),
SUM, AVG, MIN and MAX, and `(SELECT ...)` subqueries that give one value. Conditions can also use
`e [NOT] IN (SELECT ...)` and `[NOT] EXISTS (SELECT ...)`. A subquery can use the columns of the query around it.
Aggregates without GROUP BY make one row; a column that is not grouped takes its value from the group's first row.
ORDER BY and HAVING can use the items' AS names.
"""
import operator
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple, NoReturn, Type

from ..helpers.vector import Vector


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
    "vector": Vector,
}

SIMILARITY = {"COSINE": "cosine", "DOT": "dot"}  # ORDER BY function -> Table.nearest metric
AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
FUNCTIONS = {"COALESCE": (1, None), "IFNULL": (2, 2), "LOWER": (1, 1), "UPPER": (1, 1), "LENGTH": (1, 1),
             "ABS": (1, 1), "ROUND": (1, 2), "COSINE": (2, 2), "DOT": (2, 2)}  # name -> (fewest, most arguments)
# Words that end a SELECT item or source, so a name after them is not read as an alias
CLAUSE_WORDS = {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "JOIN", "INNER", "LEFT", "CROSS",
                "OUTER", "ON", "AS"}

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
    | (?P<op><=|>=|!=|<>|==|[=<>(),*;\[\]\-+/.])
    | (?P<bad>.)
""", re.VERBOSE | re.DOTALL)


class Token(NamedTuple):
    kind: str  # "name", "qname" (backtick-quoted name), "str", "num", "op" or "end"
    value: Any
    pos: int


def tokenize(text: str) -> list[Token]:
    # One C-level scan: every character matches some group, so the matches cover the text without gaps
    tokens = []
    for m in _TOKEN.finditer(text):
        kind = m.lastgroup
        if kind == "ws":
            continue
        raw, pos = m.group(), m.start()
        if kind == "num":
            tokens.append(Token(kind, float(raw) if "." in raw or "e" in raw or "E" in raw else int(raw), pos))
        elif kind == "str":
            tokens.append(Token(kind, raw[1:-1].replace(raw[0] * 2, raw[0]), pos))
        elif kind == "qname":
            tokens.append(Token(kind, raw[1:-1].replace("``", "`"), pos))
        elif kind == "bad":
            raise QueryError(f"Unexpected character {raw!r} at position {pos}")
        else:
            tokens.append(Token(kind, raw, pos))
    tokens.append(Token("end", None, len(text)))
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
    table: str | None = None  # the alias in `alias.name`

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


def hashable(v):
    """A value as a dict key: lists, vectors and dicts become tuples."""
    if isinstance(v, (list, tuple)):
        return tuple(hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, hashable(x)) for k, x in v.items()))
    if isinstance(v, (bytearray, memoryview)):
        return bytes(v)
    return v


def _floats(v) -> list[float]:
    return list(Vector.unpack(v)) if isinstance(v, (bytes, bytearray, memoryview, str)) else [float(x) for x in v]


def _similarity(metric: str, a, b) -> float | None:
    if a is None or b is None:
        return None
    a, b = _floats(a), _floats(b)
    if len(a) != len(b):
        raise QueryError(f"{metric}: the vectors have different lengths ({len(a)} and {len(b)})")
    dot = sum(x * y for x, y in zip(a, b))
    if metric == "DOT":
        return dot
    norm = (sum(x * x for x in a) * sum(y * y for y in b)) ** 0.5
    return dot / norm if norm else 0.0


ARITHMETIC = {"+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv}


@dataclass
class Arith(Expr):
    left: Expr
    op: str
    right: Expr

    def eval(self, row):
        a, b = self.left.eval(row), self.right.eval(row)
        if a is None or b is None or (self.op == "/" and b == 0):
            return None
        try:
            return ARITHMETIC[self.op](a, b)
        except TypeError:
            raise QueryError(f"Can't compute {a!r} {self.op} {b!r}") from None

    def columns(self):
        return self.left.columns() | self.right.columns()


@dataclass
class Neg(Expr):
    item: Expr

    def eval(self, row):
        v = self.item.eval(row)
        try:
            return None if v is None else -v
        except TypeError:
            raise QueryError(f"Can't negate {v!r}") from None

    def columns(self):
        return self.item.columns()


@dataclass
class Func(Expr):
    name: str  # upper case, a key of FUNCTIONS
    args: list[Expr]

    def eval(self, row):
        args = [a.eval(row) for a in self.args]
        name = self.name
        if name in ("COALESCE", "IFNULL"):
            return next((a for a in args if a is not None), None)
        if args[0] is None:
            return None
        try:
            if name == "LOWER":
                return str(args[0]).lower()
            if name == "UPPER":
                return str(args[0]).upper()
            if name == "LENGTH":
                return len(args[0])
            if name == "ABS":
                return abs(args[0])
            if name == "ROUND":
                return round(args[0], int(args[1]) if len(args) > 1 and args[1] is not None else 0)
            return _similarity(name, args[0], args[1])
        except (TypeError, ValueError) as e:
            raise QueryError(f"{name}: {e}") from None

    def columns(self):
        return set().union(*(a.columns() for a in self.args))


@dataclass
class Aggregate(Expr):
    """COUNT, SUM, AVG, MIN or MAX. The query engine computes it for each group and puts it in the group's row
    under `key`."""
    func: str
    arg: Expr | None  # None for COUNT(*)
    distinct: bool = False
    key: str = ""

    def eval(self, row):
        return row[self.key]

    def compute(self, rows: list[dict]):
        if self.arg is None:
            return len(rows)
        vals = [v for v in (self.arg.eval(r) for r in rows) if v is not None]
        if self.distinct:
            seen = {}
            for v in vals:
                seen.setdefault(hashable(v), v)
            vals = list(seen.values())
        if self.func == "COUNT":
            return len(vals)
        if not vals:
            return None
        try:
            if self.func == "SUM":
                return sum(vals)
            if self.func == "AVG":
                return sum(vals) / len(vals)
            return (min if self.func == "MIN" else max)(vals)
        except TypeError:
            raise QueryError(f"Can't compute {self.func}: the values have different types") from None

    def columns(self):
        return set() if self.arg is None else self.arg.columns()


@dataclass
class Subquery(Expr):
    """`(SELECT ...)` used as one value. The query engine sets `run` (row -> value) before it evaluates it."""
    query: Any
    run: Any = field(default=None, compare=False, repr=False)

    def eval(self, row):
        if self.run is None:
            raise QueryError("A subquery can only be used in a SELECT, or in UPDATE and DELETE without outer columns")
        return self.run(row)


@dataclass
class InSelect(Expr):
    """`operand [NOT] IN (SELECT ...)`; `run` gives the set of the subquery's values."""
    operand: Expr
    query: Any
    negate: bool = False
    run: Any = field(default=None, compare=False, repr=False)

    def eval(self, row):
        if self.run is None:
            raise QueryError("A subquery can only be used in a SELECT, or in UPDATE and DELETE without outer columns")
        return (hashable(self.operand.eval(row)) in self.run(row)) != self.negate

    def columns(self):
        return self.operand.columns()


@dataclass
class Exists(Expr):
    """`EXISTS (SELECT ...)`; `run` tells if the subquery has a row."""
    query: Any
    run: Any = field(default=None, compare=False, repr=False)

    def eval(self, row):
        if self.run is None:
            raise QueryError("A subquery can only be used in a SELECT, or in UPDATE and DELETE without outer columns")
        return self.run(row)


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
    similar: tuple[str, str, list, bool] | None = None  # ORDER BY COSINE/DOT: (metric, column, query, descending)
    limit: int | None = None
    offset: int = 0


@dataclass
class Star:
    """`*` or `alias.*` in a SELECT list."""
    table: str | None = None


@dataclass
class Source:
    """A table or a `(SELECT ...)` in FROM or JOIN. `join` is None for the first source."""
    table: str | None
    query: Any = None  # a SelectQuery for a derived table
    alias: str | None = None
    join: str | None = None  # "INNER", "LEFT" or "CROSS"
    on: Expr | None = None

    @property
    def name(self) -> str:
        return self.alias or self.table


@dataclass
class SelectQuery:
    """A SELECT that needs more than one table's rows as they are: joins, expressions, aggregates, GROUP BY,
    DISTINCT or subqueries. A SELECT without these parses to `Select`, which runs on the table's search tree."""
    items: list[tuple[Any, str | None, str]]  # (Expr or Star, AS name or None, the item's text)
    sources: list[Source]
    distinct: bool = False
    where: Expr | None = None
    group: list[Expr] = field(default_factory=list)
    having: Expr | None = None
    order: list[tuple[Expr, bool]] = field(default_factory=list)  # (expression, descending)
    similar: tuple[str, Column, list, bool] | None = None  # ORDER BY COSINE/DOT(col, [...])
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


Statement = (CreateTable | DropTable | AddColumn | DropColumn | AlterColumn | Insert | Select | SelectQuery | Update
             | Delete | ShowTables | Describe | RenameColumn | RenameTable | CopyTable | Truncate | Compact
             | Use | ShowFiles | Import | Export)


def parse(text: str) -> list[Statement]:
    """Parse one or more `;`-separated statements."""
    return Parser(text).script()


class Parser():
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0
        self.aggregates = 0  # numbers the aggregates for their keys

    def peek(self, n: int = 1) -> Token:
        return self.tokens[min(self.i + n, len(self.tokens) - 1)]

    def is_op(self, op: str, n: int = 0) -> bool:
        t = self.peek(n)
        return t.kind == "op" and t.value == op

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
        if self.accept_op("["):
            return self.numbers()
        self.error("Expected a value")

    def numbers(self) -> list:
        """The numbers of a `[x, ...]` vector literal, after its opening bracket."""
        items = []
        if not self.accept_op("]"):
            while True:
                negative = bool(self.accept_op("-"))
                if self.tok.kind != "num":
                    self.error("Expected a number")
                items.append(-self.advance().value if negative else self.advance().value)
                if not self.accept_op(","):
                    break
            self.expect_op("]")
        return items

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
        return _simple(self.query())

    def query(self) -> "SelectQuery":
        """A SELECT after its SELECT keyword."""
        distinct = bool(self.is_kw("DISTINCT") and not self.is_kw_at(1, "FROM") and not self.is_op(",", 1)
                        and self.advance())
        items = [self.item()]
        while self.accept_op(","):
            items.append(self.item())
        self.expect_kw("FROM")
        q = SelectQuery(items, [self.source(None)], distinct)
        while True:
            if self.accept_op(","):
                q.sources.append(self.source("CROSS"))
                continue
            kind = self.accept_kw("JOIN", "INNER", "LEFT", "CROSS")
            if not kind:
                break
            if kind == "LEFT":
                self.accept_kw("OUTER")
            if kind != "JOIN":
                self.expect_kw("JOIN")
            src = self.source("INNER" if kind == "JOIN" else kind)
            if src.join != "CROSS" and self.accept_kw("ON"):
                src.on = self.expr()
            elif src.join == "LEFT":
                self.error("Expected ON")
            q.sources.append(src)
        q.where = self.where()
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            q.group = [self.additive()]
            while self.accept_op(","):
                q.group.append(self.additive())
        if self.accept_kw("HAVING"):
            q.having = self.expr()
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            if self.tok.kind == "name" and self.tok.value.upper() in SIMILARITY and self.is_op("(", 1):
                metric = SIMILARITY[self.advance().value.upper()]
                self.expect_op("(")
                column = self.column()
                self.expect_op(",")
                self.expect_op("[")
                query = self.numbers()
                self.expect_op(")")
                q.similar = (metric, column, query, self.accept_kw("ASC", "DESC") != "ASC")
                if self.tok.kind == "op" and self.tok.value == ",":
                    self.error(f"{metric.upper()}(...) must be the only ORDER BY key")
            else:
                while True:
                    q.order.append((self.additive(), self.accept_kw("ASC", "DESC") == "DESC"))
                    if not self.accept_op(","):
                        break
        if self.accept_kw("LIMIT"):
            q.limit = self.whole_number()
            if self.accept_kw("OFFSET"):
                q.offset = self.whole_number()
        return q

    def is_kw_at(self, n: int, *words: str) -> bool:
        t = self.peek(n)
        return t.kind == "name" and t.value.upper() in words

    def alias(self) -> str | None:
        """An optional `[AS] name` after a SELECT item or a source."""
        if self.accept_kw("AS"):
            return self.ident("a name after AS")
        t = self.tok
        if t.kind == "qname" or (t.kind == "name" and t.value.upper() not in RESERVED | CLAUSE_WORDS):
            return self.advance().value
        return None

    def item(self) -> tuple:
        if self.accept_op("*"):
            return Star(), None, "*"
        if self.tok.kind in ("name", "qname") and self.is_op(".", 1) and self.is_op("*", 2):
            table = self.advance().value
            self.advance(), self.advance()
            return Star(table), None, f"{table}.*"
        start = self.tok.pos
        e = self.additive()
        text = self.text[start:self.tok.pos].strip()
        return e, self.alias(), text

    def source(self, join: str | None) -> Source:
        if self.accept_op("("):
            self.expect_kw("SELECT")
            q = self.query()
            self.expect_op(")")
            alias = self.alias()
            if alias is None:
                self.error("Expected an alias for the subquery")
            return Source(None, q, alias, join)
        return Source(self.ident("a table name"), None, self.alias(), join)

    def column(self) -> Column:
        name = self.ident("a column name")
        if self.accept_op("."):
            return Column(self.ident("a column name"), name)
        return Column(name)

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
        if self.is_op("(") and not self.is_kw_at(1, "SELECT"):
            # A condition in parentheses, unless it is a value in parentheses, such as `(a + b) * 2 > c`
            start = self.i
            self.advance()
            try:
                e = self.expr()
                self.expect_op(")")
            except QueryError:
                e = None
            t = self.tok
            if e is not None and not ((t.kind == "op" and t.value in {*OPERATORS, *ARITHMETIC})
                                      or self.is_kw("IS", "IN", "LIKE", "NOT")):
                return e
            self.i = start
        if self.is_kw("EXISTS") and self.is_op("(", 1) and self.is_kw_at(2, "SELECT"):
            self.advance(), self.advance(), self.advance()
            q = self.query()
            self.expect_op(")")
            return Exists(q)
        left = self.additive()
        if self.accept_kw("IS"):
            negate = bool(self.accept_kw("NOT"))
            self.expect_kw("NULL", "NONE")
            return IsNull(left, negate)
        negate = bool(self.accept_kw("NOT"))
        if self.accept_kw("IN"):
            self.expect_op("(")
            if self.accept_kw("SELECT"):
                q = self.query()
                self.expect_op(")")
                return InSelect(left, q, negate)
            values = [self.additive()]
            while self.accept_op(","):
                values.append(self.additive())
            self.expect_op(")")
            return In(left, values, negate)
        if self.accept_kw("LIKE"):
            if self.tok.kind != "str":
                self.error("Expected a string pattern")
            return Like(left, self.advance().value, negate)
        if negate:
            self.error("Expected IN or LIKE")
        op = self.expect_op(*OPERATORS)
        return Compare(left, op, self.additive())

    def additive(self) -> Expr:
        e = self.term()
        while (op := self.accept_op("+", "-")):
            e = Arith(e, op, self.term())
        return e

    def term(self) -> Expr:
        e = self.factor()
        while (op := self.accept_op("*", "/")):
            e = Arith(e, op, self.factor())
        return e

    def factor(self) -> Expr:
        if self.is_op("-") and self.peek().kind != "num":
            self.advance()
            return Neg(self.factor())
        return self.operand()

    def operand(self) -> Expr:
        t = self.tok
        if t.kind == "op" and t.value == "(":
            self.advance()
            if self.accept_kw("SELECT"):
                e = Subquery(self.query())
            else:
                e = self.additive()
            self.expect_op(")")
            return e
        if t.kind == "name" and self.is_op("(", 1):
            name = t.value.upper()
            if name in AGGREGATES:
                return self.aggregate()
            if name in FUNCTIONS:
                self.advance(), self.advance()
                args = [self.additive()]
                while self.accept_op(","):
                    args.append(self.additive())
                self.expect_op(")")
                fewest, most = FUNCTIONS[name]
                if len(args) < fewest or (most is not None and len(args) > most):
                    raise QueryError(f"{name} takes {fewest if fewest == most else f'{fewest} or more'} "
                                     f"argument{'s' if most != 1 else ''}, got {len(args)}")
                return Func(name, args)
        if t.kind == "qname" or (t.kind == "name" and t.value.upper() not in RESERVED):
            return self.column()
        return Literal(self.literal())

    def aggregate(self) -> Aggregate:
        func = self.advance().value.upper()
        self.advance()
        self.aggregates += 1
        key = f"\0{func}{self.aggregates}"
        if func == "COUNT" and self.accept_op("*"):
            self.expect_op(")")
            return Aggregate(func, None, False, key)
        distinct = bool(self.accept_kw("DISTINCT"))
        arg = self.additive()
        self.expect_op(")")
        return Aggregate(func, arg, distinct, key)


LEGACY_EXPRS = (Column, Literal, Compare, And, Or, Not, IsNull, In, Like)


def plain(e) -> bool:
    """True when `e` uses only what the one-table SELECT path runs: unqualified columns, values and conditions."""
    if isinstance(e, Column):
        return e.table is None
    if isinstance(e, Literal):
        return True
    if isinstance(e, Compare):
        return all(isinstance(x, (Column, Literal)) and plain(x) for x in (e.left, e.right))
    if isinstance(e, In):
        return all(isinstance(x, (Column, Literal)) and plain(x) for x in (e.operand, *e.values))
    if isinstance(e, (And, Or)):
        return all(plain(x) for x in e.items)
    if isinstance(e, Not):
        return plain(e.item)
    if isinstance(e, (IsNull, Like)):
        return isinstance(e.operand, (Column, Literal)) and plain(e.operand)
    return False


def _simple(q: SelectQuery) -> "Select | SelectQuery":
    """A `Select` for a query that the one-table path can run (with the table's search tree), else `q`."""
    src = q.sources[0]
    if len(q.sources) > 1 or src.table is None or src.alias or q.distinct or q.group or q.having:
        return q
    if q.where is not None and not plain(q.where):
        return q
    if any(not (isinstance(e, Column) and e.table is None) for e, _ in q.order):
        return q
    if q.similar and q.similar[1].table is not None:
        return q
    count, columns = False, None
    (first, first_alias, _), n = q.items[0], len(q.items)
    if n == 1 and first == Star():
        pass
    elif n == 1 and isinstance(first, Aggregate) and first.func == "COUNT" and first.arg is None and first_alias is None:
        count = True
    elif all(isinstance(e, Column) and e.table is None and alias is None for e, alias, _ in q.items):
        columns = [e.name for e, _, _ in q.items]
    else:
        return q
    similar = q.similar and (q.similar[0], q.similar[1].name, q.similar[2], q.similar[3])
    return Select(src.table, columns, count, q.where, [(e.name, d) for e, d in q.order], similar or None,
                  q.limit, q.offset)
