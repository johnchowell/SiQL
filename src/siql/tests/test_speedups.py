"""Tests for the search primitives: the C versions (when compiled) must match the Python ones exactly, and the
interpreter's scan-based WHERE and ORDER BY must match plain row-by-row evaluation."""
import os
import random
import re
import tempfile
import unittest

from ..helpers import search
from ..helpers.search import (py_scan, py_scan_match, py_values, EQ, NE, LT, LE, GT, GE, IS_NULL, NOT_NULL)
from ..models import Table, Cell
from ..controllers import Interpreter, QueryError, parse

try:
    from .. import _speedups
except ImportError:
    _speedups = None

NAN = float("nan")


class Picky:
    """Equal to everything, and refuses ordering with a ValueError (not a TypeError)."""
    def __eq__(self, other):
        return True

    def __lt__(self, other):
        raise ValueError("no ordering")

    __hash__ = object.__hash__


class SpeedupsBuildTests(unittest.TestCase):
    def test_required_build_is_present(self):
        # CI sets SIQL_REQUIRE_SPEEDUPS=1 so a failed compile can't silently fall back to Python
        if os.environ.get("SIQL_REQUIRE_SPEEDUPS") == "1":
            self.assertIsNotNone(_speedups, "the C extension wasn't built")
            self.assertTrue(search.SPEEDUPS or os.environ.get("SIQL_PURE_PYTHON") == "1")

    def test_pure_python_switch(self):
        if os.environ.get("SIQL_PURE_PYTHON") == "1":
            self.assertFalse(search.SPEEDUPS)
            self.assertIs(search.SkipList, search.PySkipList)


@unittest.skipIf(_speedups is None, "C extension not built")
class CMatchesPythonTests(unittest.TestCase):
    VALUES = [None, 0, 1, -1, 2.5, 1.0, True, False, NAN, float("inf"), "", "a", "ab", "B", "1", [1], (1, 2), {"k": 1}]

    def cells(self, values):
        return [Cell(v) for v in values]

    def test_scan(self):
        rng = random.Random(3)
        for _ in range(40):
            cells = self.cells(rng.choices(self.VALUES, k=60))
            for op in (EQ, NE, LT, LE, GT, GE, IS_NULL, NOT_NULL):
                for value in self.VALUES:
                    for value_first in (False, True):
                        with self.subTest(op=op, value=value, value_first=value_first):
                            self.assertEqual(_speedups.scan(cells, op, value, value_first),
                                             py_scan(cells, op, value, value_first))

    def test_scan_errors_other_than_type_errors_propagate(self):
        cells = self.cells([1, Picky(), 2])
        for scan in (_speedups.scan, py_scan):
            with self.subTest(scan=scan):
                self.assertEqual(scan(cells, EQ, 5), [1])
                with self.assertRaises(ValueError):
                    scan(cells, LT, 5)

    def test_scan_match_and_values(self):
        rng = random.Random(4)
        patterns = [re.compile(p, re.IGNORECASE | re.DOTALL).fullmatch for p in ("a.*", ".", "", ".*b", "1")]
        for _ in range(40):
            cells = self.cells(rng.choices(self.VALUES, k=60))
            self.assertEqual(_speedups.values(cells), py_values(cells))
            for fullmatch in patterns:
                for negate in (False, True):
                    self.assertEqual(_speedups.scan_match(cells, fullmatch, negate),
                                     py_scan_match(cells, fullmatch, negate))

    def test_bad_arguments(self):
        with self.assertRaises(TypeError):
            _speedups.scan((Cell(1),), EQ, 1)  # must be a list
        with self.assertRaises(ValueError):
            _speedups.scan([Cell(1)], 99, 1)
        with self.assertRaises(AttributeError):
            _speedups.scan([object()], EQ, 1)
        with self.assertRaises(TypeError):
            _speedups.values("not a list")
        with self.assertRaises(TypeError):
            _speedups.SkipList(1)


class InterpreterScanTests(unittest.TestCase):
    """WHERE answered by scans and the tree, and ORDER BY on row numbers, against plain row-by-row evaluation."""
    LITERALS = ["1", "1.5", "-2", "0", "'x'", "'ab'", "'A'", "''", "TRUE", "FALSE", "NULL"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Interpreter(self._tmp.name)
        self.rng = random.Random(9)
        self.db.execute("CREATE TABLE r (a any, b any, n float, s str)")
        values = [1, 1.5, -2, 0, 7, "x", "ab", "A", "xa", "", True, False, None]
        rows = []
        for _ in range(150):
            a, b = self.rng.choice(values), self.rng.choice(values)
            n = self.rng.choice([None, 0, 1, 2.5, -3, 10])
            s = self.rng.choice([None, "a", "b", "ab", "B", ""])
            rows.append(f"({self.literal(a)}, {self.literal(b)}, {self.literal(n)}, {self.literal(s)})")
        self.db.execute("INSERT INTO r VALUES " + ", ".join(rows))
        self.table = self.db.database.table("r")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def literal(v):
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        return f"'{v}'" if isinstance(v, str) else repr(v)

    def condition(self, depth=0):
        rng = self.rng
        kind = rng.random()
        if depth < 2 and kind < 0.3:
            joiner = rng.choice(["AND", "OR"])
            return f"({self.condition(depth + 1)}) {joiner} ({self.condition(depth + 1)})"
        if depth < 2 and kind < 0.4:
            return f"NOT ({self.condition(depth + 1)})"
        column = rng.choice("ab")
        template = rng.choice([
            "{c} {op} {lit}", "{lit} {op} {c}", "{c} {op} {lit}", "{c} IS NULL", "{c} IS NOT NULL",
            "{c} LIKE 'a%'", "{c} NOT LIKE '%a'", "{c} IN ({lit}, {lit})", "{c} NOT IN ({lit})", "a = b", "1 = 1",
        ])
        op = rng.choice(["=", "!=", "<", "<=", ">", ">="])
        return template.replace("{c}", column).replace("{op}", op).replace("{lit}", rng.choice(self.LITERALS), 1) \
            .replace("{lit}", rng.choice(self.LITERALS))

    def brute_force(self, where):
        names = self.table.struct.column_names
        return [i for i, row in enumerate(self.table.rows)
                if where.eval({n: row.col(j).value for j, n in enumerate(names)})]

    def test_where_matches_row_by_row(self):
        for tree in (True, False):
            self.table.tree = tree
            for _ in range(300):
                text = self.condition()
                (statement,) = parse(f"SELECT * FROM r WHERE {text}")
                with self.subTest(tree=tree, where=text):
                    self.assertEqual(self.db._matching(self.table, statement.where), self.brute_force(statement.where))

    def test_order_by_matches_sorting_whole_rows(self):
        names = self.table.struct.column_names
        records = [{n: row.col(j).value for j, n in enumerate(names)} for row in self.table.rows]
        for order in ["n", "n DESC", "s", "s DESC, n", "n, s DESC", "s DESC, n DESC"]:
            for limit in ["", " LIMIT 7", " LIMIT 5 OFFSET 140", " LIMIT 0"]:
                with self.subTest(order=order, limit=limit):
                    got = self.db.execute(f"SELECT * FROM r ORDER BY {order}{limit}").rows
                    expected = list(records)
                    for part in reversed(order.split(", ")):
                        column, _, direction = part.partition(" ")
                        expected.sort(key=lambda r: (r[column] is not None, r[column]), reverse=direction == "DESC")
                    _, _, rest = limit.partition("LIMIT ")
                    if rest:
                        count, _, offset = rest.partition(" OFFSET ")
                        start = int(offset or 0)
                        expected = expected[start:start + int(count)]
                    self.assertEqual(got, [[r[n] for n in names] for r in expected])

    def test_order_by_mixed_types_is_still_an_error(self):
        with self.assertRaises(QueryError):
            self.db.execute("SELECT * FROM r ORDER BY a")

    def test_count_with_order_and_limit(self):
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM r WHERE n > 0 ORDER BY n LIMIT 3").rows, [[3]])


if __name__ == "__main__":
    unittest.main()
