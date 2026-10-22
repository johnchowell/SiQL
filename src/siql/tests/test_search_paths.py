"""Ordered range reads, selective AND planning, per-value cache invalidation and the tokenizer."""
import math
import operator
import random
import tempfile
import unittest
from unittest import mock

from .. import Table, TableDiff
from ..models import AddRow, DropRow, ColumnTree
from ..models.tree import order_key
from ..helpers import search
from ..helpers.search import PySkipList
from ..controllers import Interpreter, interpreter
from ..controllers.parser import QueryError, Token, parse, tokenize

try:
    from .._speedups import SkipList as CSkipList
except ImportError:
    CSkipList = None
SKIP_LISTS = [PySkipList] + ([CSkipList] if CSkipList else [])

STORED = [-5, -1, 0, 1, 2, 7, 10, 15, 123, -0.0, 0.5, 1.0, 2.5, -2.5, 1e3, True, False,
          "", "a", "ab", "abc", "b", "B", "1x", "10", "z", "\u00e9", None]
VALUES = STORED + [math.inf, -math.inf]  # table files can't hold infinities; cells can
BOUNDS = [-5, 0, 1, 2, 2.5, 10, 100, math.inf, -math.inf, True, "", "a", "ab", "b", "1", "1x", "y", "\u00e9"]


def between(value, low, high, low_inclusive, high_inclusive):
    """The scan comparisons: a value that can't be compared doesn't match."""
    try:
        return ((low is None or (value >= low if low_inclusive else value > low))
                and (high is None or (value <= high if high_inclusive else value < high)))
    except TypeError:
        return False


class EqualToFive:
    def __eq__(self, other):
        return other == 5

    __hash__ = None


class SkipListBetweenTests(unittest.TestCase):
    def test_between_matches_sorted_keys(self):
        for SkipList in SKIP_LISTS:
            with self.subTest(SkipList=SkipList):
                rng = random.Random(7)
                lanes, keys = SkipList(), {}
                for cell_id in range(2000):
                    key = order_key(rng.choice([rng.randrange(-300, 300), rng.random() * 600 - 300,
                                                "".join(rng.choice("abc") for _ in range(rng.randrange(4)))]))
                    lanes.insert(key, cell_id)
                    keys.setdefault(key, set()).add(cell_id)
                ordered = sorted(keys)
                for _ in range(300):
                    low, high = (rng.choice(ordered + [None, (0, 0.25), (1, "b")]) for _ in range(2))
                    low_inclusive, high_inclusive = rng.random() < 0.5, rng.random() < 0.5
                    expected = [keys[k] for k in ordered if between(k, low, high, low_inclusive, high_inclusive)]
                    self.assertEqual(lanes.between(low, high, low_inclusive, high_inclusive), expected)
                    count = sum(map(len, expected))
                    self.assertEqual(lanes.between(low, high, low_inclusive, high_inclusive, count), expected)
                    if count:
                        self.assertIsNone(lanes.between(low, high, low_inclusive, high_inclusive, count - 1))

    def test_between_empty_and_errors(self):
        for SkipList in SKIP_LISTS:
            with self.subTest(SkipList=SkipList):
                lanes = SkipList()
                self.assertEqual(lanes.between(), [])
                self.assertEqual(lanes.between(1, 2, limit=0), [])
                lanes.insert(1, 1)
                with self.assertRaises(TypeError):
                    lanes.between("x")


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def expected(self, table, column, *bounds):
        return [i for i, cell in enumerate(table.cols[column]) if between(cell.value, *bounds)]

    def random_bounds(self, rng):
        low, high = rng.choice(BOUNDS + [None]), rng.choice(BOUNDS + [None])
        return low, high, rng.random() < 0.5, rng.random() < 0.5

    def test_random_mutations_match_scans(self):
        for lazy_delete, lazy_add in [(False, False), (True, False), (False, True), (True, True)]:
            with self.subTest(lazy_delete=lazy_delete, lazy_add=lazy_add):
                rng = random.Random(31)
                table = Table(file=f"{self.tmp.name}/{lazy_delete}{lazy_add}.siql", lazy_delete=lazy_delete,
                              lazy_add=lazy_add)
                table.addCols(v=object)
                TableDiff([AddRow(i, {"v": rng.choice(STORED)}) for i in range(300)]).apply(table)
                for step in range(400):
                    action = rng.random()
                    if action < 0.3:
                        table.add(v=rng.choice(STORED))
                    elif action < 0.5 and table.rows:
                        TableDiff([DropRow(rng.randrange(len(table.rows)))]).apply(table)
                    elif action < 0.7 and table.rows:
                        rng.choice(table.cols["v"]).value = rng.choice(VALUES)
                    bounds = self.random_bounds(rng)
                    self.assertEqual(table.find_range("v", *bounds), self.expected(table, "v", *bounds), bounds)
                    probe = rng.choice(VALUES)
                    self.assertEqual(table.find("v", probe),
                                     [i for i, cell in enumerate(table.cols["v"]) if probe == cell.value])

    def test_the_lanes_answer_builtin_columns(self):
        table = Table(file=f"{self.tmp.name}/lanes.siql")
        table.addCols(v=object)
        TableDiff([AddRow(i, {"v": value}) for i, value in enumerate(STORED * 4)]).apply(table)
        for value in [math.inf, -math.inf] * 4:
            table.add(v=None)
            table.rows[-1].col("v").value = value
        node = table.index["v"]
        for bounds in [(1, 10, True, False), (None, "b", True, True), ("a", None, False, True),
                       (-math.inf, 0, True, True), (None, math.inf, True, False)]:
            with self.subTest(bounds=bounds):
                rows = node.find_range(*bounds)
                self.assertIsNotNone(rows)
                positions = sorted(table.rows.index(row) for row in rows)
                self.assertEqual(positions, self.expected(table, "v", *bounds))
        self.assertIsNone(node.find_range(1, "b"))  # mixed kinds compare by scan
        self.assertIsNone(node.find_range())
        self.assertIsNone(node.find_range(float("nan")))
        self.assertIsNone(node.find_range(None, 100, limit=3))
        self.assertEqual(len(node.find_range(100, None, limit=12)), 12)  # 123, 1e3 and inf, four times each
        self.assertIsNone(node.find_range(100, None, limit=11))

    def test_custom_and_unordered_values_fall_back_to_scans(self):
        table = Table(file=f"{self.tmp.name}/custom.siql")
        table.addCols(v=object)
        TableDiff([AddRow(i, {"v": value}) for i, value in enumerate(STORED)]).apply(table)
        table.rows[0].col("v").value = [1, 2]
        node = table.index["v"]
        self.assertIsNone(node.find_range(0, 10))
        for _ in range(3):
            bounds = (0, 10, True, True)
            self.assertEqual(table.find_range("v", *bounds), self.expected(table, "v", *bounds))
        table.rows[0].col("v").value = 3
        self.assertIsNotNone(node.find_range(0, 10))
        self.assertEqual(table.find_range("v", 0, 10), self.expected(table, "v", 0, 10, True, True))
        table.tree = False
        self.assertEqual(table.find_range("v", "a", "b", False), self.expected(table, "v", "a", "b", False, True))
        self.assertEqual(table.find_range("v"), list(range(len(table.rows))))

    def test_none_branch_exact_lookups(self):
        table = Table(file=f"{self.tmp.name}/nulls.siql")
        table.addCols(v=object)
        TableDiff([AddRow(i, {"v": None}) for i in range(40)]).apply(table)
        for i in range(40):
            table.rows[i].col("v").value = [None, math.inf, 1, -math.inf][i % 4]
        self.assertEqual(table.find("v", None), list(range(0, 40, 4)))
        self.assertEqual(table.find("v", math.inf), list(range(1, 40, 4)))
        table.rows[0].col("v").value = math.inf
        table.rows[1].col("v").value = None
        self.assertEqual(table.find("v", None), [1] + list(range(4, 40, 4)))
        self.assertEqual(table.find("v", math.inf), [0] + list(range(5, 40, 4)))
        table.rows[2].col("v").value = EqualToFive()
        self.assertEqual(table.find("v", 5), [2])
        self.assertEqual(table.find("v", None), [1] + list(range(4, 40, 4)))


class CacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.table = Table(file=f"{self.tmp.name}/rows.siql")
        self.table.addCols(n=object)
        TableDiff([AddRow(i, {"n": i % 10}) for i in range(100)]).apply(self.table)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, value):
        return [i for i, cell in enumerate(self.table.cols["n"]) if value == cell.value]

    def test_changes_keep_results_for_other_values(self):
        for value in range(10):
            self.table.find("n", value)
        entries = self.table.cache_info["entries"]
        self.table.add(n=20)
        self.table.rows[0].col("n").value = 30
        TableDiff([DropRow(1)]).apply(self.table)  # held 1
        self.assertEqual(self.table.cache_info["entries"], entries - 2)  # 0 and 1 changed
        node = self.table.index["n"]
        with mock.patch.object(type(node), "_find", side_effect=AssertionError("unexpected tree read")):
            for value in range(2, 10):
                self.assertEqual(self.table.find("n", value), self.scan(value))

    def test_equal_builtins_share_one_result(self):
        self.assertEqual(self.table.find("n", 1), self.scan(1))
        self.table.add(n=1.0)
        self.assertEqual(self.table.find("n", True), self.scan(1))
        self.table.rows[2].col("n").value = True
        self.assertEqual(self.table.find("n", 1.0), self.scan(1))
        self.table.find("n", 3)
        self.table.rows[3].col("n").value = 3.0  # an equal value of another type
        self.assertEqual(self.table.find("n", 3), self.scan(3))

    def test_unbuiltin_values_drop_the_column(self):
        self.table.find("n", 5)
        self.table.rows[0].col("n").value = EqualToFive()
        self.assertEqual(self.table.cache_info["entries"], 0)
        self.assertEqual(self.table.find("n", 5), self.scan(5))
        self.table.rows[0].col("n").value = 0
        self.assertEqual(self.table.find("n", 5), self.scan(5))

    def test_random_mutations_with_lazy_queues(self):
        rng = random.Random(3)
        for lazy in (False, True):
            self.table.lazy_delete = self.table.lazy_add = lazy
            for _ in range(300):
                action = rng.random()
                if action < 0.3:
                    self.table.add(n=rng.choice([1, 2, 2.0, True, None, "x"]))
                elif action < 0.6 and self.table.rows:
                    TableDiff([DropRow(rng.randrange(len(self.table.rows)))]).apply(self.table)
                elif self.table.rows:
                    rng.choice(self.table.cols["n"]).value = rng.choice([1, 3, 3.0, False, None, "x"])
                queries = [0, 1, 2, 3, True, 1.0, None, "x", "y"]
                self.assertEqual(self.table.find_batch("n", queries), [self.scan(v) for v in queries])


class PlannerTests(unittest.TestCase):
    OPS = ["=", "!=", "<", "<=", ">", ">="]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Interpreter(self.tmp.name)
        self.db.execute("CREATE TABLE t (id int, a int, b str, c float)")
        rng = random.Random(17)
        rows = []
        for i in range(400):
            rows.append(f"({i}, {rng.choice(['NULL', *map(str, range(-3, 12))])}, "
                        f"{rng.choice(['NULL', *(repr(s) for s in ['', 'a', 'ab', 'b', 'B', 'ba', 'z'])])}, "
                        f"{rng.choice(['NULL', '-1.5', '0.0', '2.0', '3.5', '11.0'])})")
        self.db.execute("INSERT INTO t VALUES " + ", ".join(rows))
        self.table = self.db.database.table("t")
        self.rng = rng

    def tearDown(self):
        self.tmp.cleanup()

    def literal(self, column):
        if column == "b":
            return self.rng.choice(["''", "'a'", "'ab'", "'b'", "'c'", "NULL", "1"])
        return self.rng.choice(["-1", "0", "2", "3.5", "5", "11", "TRUE", "NULL", "'a'"])

    def atom(self):
        column = self.rng.choice("abc")
        kind = self.rng.random()
        if kind < 0.55:
            left, right = column, self.literal(column)
            if self.rng.random() < 0.3:
                left, right = right, left
            return f"{left} {self.rng.choice(self.OPS)} {right}"
        if kind < 0.7:
            values = ", ".join(self.literal(column) for _ in range(self.rng.randrange(1, 4)))
            return f"{column} {self.rng.choice(['', 'NOT '])}IN ({values})"
        if kind < 0.8:
            return f"{column} IS {self.rng.choice(['', 'NOT '])}NULL"
        if kind < 0.9:
            return f"b {self.rng.choice(['', 'NOT '])}LIKE '{self.rng.choice(['a%', '%b', '_', 'B%'])}'"
        return f"a {self.rng.choice(self.OPS)} c"

    def expression(self, depth=0):
        if depth >= 2 or self.rng.random() < 0.3:
            return self.atom()
        joiner = self.rng.choice([" AND ", " AND ", " OR "])
        parts = [self.expression(depth + 1) for _ in range(self.rng.randrange(2, 4))]
        text = "(" + joiner.join(parts) + ")"
        return "NOT " + text if self.rng.random() < 0.15 else text

    def reference(self, where):
        records = [{n: row.col(n).value for n in self.table.struct.column_names} for row in self.table.rows]
        return [[r["id"]] for r in records if where.eval(r)]

    def test_random_where_clauses_match_row_evaluation(self):
        for step in range(500):
            text = self.expression()
            query = f"SELECT id FROM t WHERE {text}"
            expected = self.reference(parse(query)[0].where)
            self.table.tree = step % 5 != 0
            self.assertEqual(self.db.execute(query).rows, expected, query)

    def test_and_reads_only_the_selective_candidates(self):
        query = "SELECT id FROM t WHERE c > -100 AND a = 4 AND b >= 'a'"
        expected = self.reference(parse(query)[0].where)
        with mock.patch.object(interpreter, "scan", wraps=search.scan) as scanned:
            self.assertEqual(self.db.execute(query).rows, expected)
        self.assertTrue(all(len(call.args[0]) < len(self.table.rows) for call in scanned.call_args_list))

    def test_two_bounds_read_one_range(self):
        query = "SELECT id FROM t WHERE id >= 100 AND 120 > id AND a != 4"
        reads, original = [], Table._index_range

        def spy(table, *args, **kwargs):
            reads.append(args)
            return original(table, *args, **kwargs)

        with mock.patch.object(Table, "_index_range", spy):
            rows = self.db.execute(query).rows
        self.assertEqual(rows, self.reference(parse(query)[0].where))
        self.assertEqual(reads, [("id", 100, 120, True, False)])

    def test_order_by_reads_matched_values(self):
        for query in ["SELECT id FROM t WHERE a = 3 ORDER BY c DESC, b LIMIT 4",
                      "SELECT id FROM t WHERE a >= 0 ORDER BY b, id DESC LIMIT 7 OFFSET 2",
                      "SELECT id FROM t ORDER BY c, a DESC"]:
            with self.subTest(query=query):
                select = parse(query)[0]
                records = [{n: row.col(n).value for n in self.table.struct.column_names} for row in self.table.rows]
                matched = [r for r in records if select.where is None or select.where.eval(r)]
                for column, descending in reversed(select.order):
                    nulls = [r for r in matched if r[column] is None]
                    rest = sorted((r for r in matched if r[column] is not None), key=operator.itemgetter(column),
                                  reverse=descending)
                    matched = rest + nulls if descending else nulls + rest
                end = None if select.limit is None else select.offset + select.limit
                self.assertEqual(self.db.execute(query).rows, [[r["id"]] for r in matched[select.offset:end]])

    def test_find_where_starts_from_the_smallest_match(self):
        rng = random.Random(5)
        for _ in range(200):
            criteria = {column: rng.choice([None, *range(-3, 12), "a", "b", 2.0, 3.5])
                        for column in rng.sample("abc", rng.randrange(1, 4))}
            expected = [i for i, row in enumerate(self.table.rows)
                        if all(value == row.col(column).value for column, value in criteria.items())]
            self.assertEqual(self.table.find_where(**criteria), expected, criteria)
        lookups, original = [], ColumnTree.find

        def spy(node, value):
            lookups.append(value)
            return original(node, value)

        row = self.table.rows[7]
        with mock.patch.object(ColumnTree, "find", spy):
            self.assertEqual(self.table.find_where(id=7, a=4, c=2.0),
                             [7] if row.col("a").value == 4 and row.col("c").value == 2.0 else [])
        self.assertEqual(lookups, [7])


class TokenizerTests(unittest.TestCase):
    def test_tokens_and_positions(self):
        self.assertEqual(tokenize("SELECT 'a''b', `x``y`,1.5e3 -- note\n FROM t"),
                         [Token("name", "SELECT", 0), Token("str", "a'b", 7), Token("op", ",", 13),
                          Token("qname", "x`y", 15), Token("op", ",", 21), Token("num", 1500.0, 22),
                          Token("name", "FROM", 37), Token("name", "t", 42), Token("end", None, 43)])
        self.assertEqual([t.value for t in tokenize("1 1.0 .5 2e1")[:4]], [1, 1.0, 0.5, 20.0])
        self.assertIs(type(tokenize("7")[0].value), int)

    def test_unexpected_characters(self):
        for text, char, pos in [("SELECT ? FROM t", "?", 7), ("SELECT * FROM t WHERE a = 'x", "'", 26),
                                ("SELECT `", "`", 7)]:
            with self.subTest(text=text):
                with self.assertRaises(QueryError) as caught:
                    parse(text)
                self.assertEqual(str(caught.exception), f"Unexpected character {char!r} at position {pos}")


if __name__ == "__main__":
    unittest.main()
