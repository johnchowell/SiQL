"""Tests for vector columns and `Table.nearest`: the cell and file forms, exact top-k search in C and in Python,
filters, cache invalidation on edits, old files, and ORDER BY COSINE/DOT in queries."""
import math
import os
import random
import tempfile
import unittest
from array import array

from ..helpers import search, vector as vector_module
from ..helpers.search import py_vector_top, py_vector_normalize
from ..helpers.vector import Vector, to_text, from_text
from ..models import Table, TableDiff, AddCol, SetColType, AddRow, DropRow, SetCell, DropCol, RenameCol
from ..controllers import Interpreter, QueryError

try:
    from .. import _speedups
except ImportError:
    _speedups = None


def f32(values) -> bytes:
    return array("f", values).tobytes()


def read(path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def reference(rows, query, k=None, metric="cosine", positions=None):
    """(row, score) pairs, best first, ties by lower row, computed in doubles from float32 values."""
    q = list(array("f", f32(query)))
    if metric == "cosine":
        q = unit(q)
    scored = []
    for i, v in enumerate(rows):
        if v is None or (positions is not None and i not in positions):
            continue
        v = list(array("f", f32(v)))
        if metric == "cosine":
            v = unit(v)
        scored.append((-sum(a * b for a, b in zip(v, q)), i))
    scored.sort()
    return [(i, -s) for s, i in scored[:k]]


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def assertPairs(self, got, want, places=5):
        self.assertEqual([i for i, _ in got], [i for i, _ in want])
        for (_, a), (_, b) in zip(got, want):
            self.assertAlmostEqual(a, b, places=places)


class PackTests(unittest.TestCase):
    def test_forms(self):
        want = f32([1.5, -2.0, 0.25])
        self.assertEqual(Vector.pack([1.5, -2, 0.25]), want)
        self.assertEqual(Vector.pack((1.5, -2.0, 0.25)), want)
        self.assertEqual(Vector.pack(array("f", [1.5, -2.0, 0.25])), want)
        self.assertEqual(Vector.pack(array("d", [1.5, -2.0, 0.25])), want)
        self.assertEqual(Vector.pack(want), want)
        self.assertEqual(Vector.pack(bytearray(want)), want)
        self.assertEqual(Vector.pack(to_text(want)), want)
        self.assertEqual(Vector.pack(memoryview(want).cast("f")), want)
        self.assertIsNone(Vector.pack(None))
        self.assertEqual(list(Vector.unpack(want)), [1.5, -2.0, 0.25])
        self.assertIsNone(Vector.unpack(None))

    def test_numpy(self):
        try:
            import numpy
        except ImportError:
            self.skipTest("numpy is not installed")
        a = numpy.array([1.5, -2.0, 0.25], dtype=numpy.float32)
        self.assertEqual(Vector.pack(a), f32([1.5, -2.0, 0.25]))
        self.assertEqual(Vector.pack(a.astype(">f4")), f32([1.5, -2.0, 0.25]))
        self.assertEqual(Vector.pack(a.astype(numpy.float64)), f32([1.5, -2.0, 0.25]))
        with self.assertRaises(ValueError):
            Vector.pack(numpy.zeros((2, 2)))

    def test_bad_values(self):
        for bad in ([], b"", b"abc", "not base64!"):
            with self.assertRaises(ValueError):
                Vector.pack(bad)
        for bad in (3, ["a"], {"x": 1}):
            with self.assertRaises(TypeError):
                Vector.pack(bad)

    def test_text_form(self):
        data = f32([1.0, 2.0])
        self.assertEqual(from_text(to_text(data)), data)
        self.assertEqual(to_text(data), "AACAPwAAAEA=")  # little-endian float32 bytes in base64


class KernelTests(unittest.TestCase):
    """The C kernel and the Python one give the same rows and scores."""
    def kernels(self):
        found = [(py_vector_top, py_vector_normalize)]
        if _speedups is not None:
            found.append((_speedups.vector_top, _speedups.vector_normalize))
        return found

    def test_top_k_matches_reference(self):
        rng = random.Random(7)
        dim, n = 37, 300
        rows = [[rng.uniform(-1, 1) for _ in range(dim)] for _ in range(n)]
        query = [rng.uniform(-1, 1) for _ in range(dim)]
        for top, normalize in self.kernels():
            matrix = bytearray(b"".join(f32(r) for r in rows))
            normalize(matrix, dim)
            q = bytearray(f32(query))
            normalize(q, dim)
            for k in (1, 5, n, n + 10, -1):
                got = top(matrix, dim, q, k)
                want = reference(rows, query, None if k == -1 else k)
                self.assertEqual([i for i, _ in got], [i for i, _ in want], (top, k))
                for (_, a), (_, b) in zip(got, want):
                    self.assertAlmostEqual(a, b, places=5)
            cand = array("q", [5, 9, 100, 3])
            got = top(matrix, dim, q, 2, cand)
            self.assertEqual([i for i, _ in got], [i for i, _ in reference(rows, query, 2, positions={5, 9, 100, 3})])

    def test_ties_nan_and_errors(self):
        dim = 2
        rows = [[1, 0], [float("nan"), 0], [1, 0], [0, 1], [1, 0]]
        for top, normalize in self.kernels():
            matrix = bytearray(b"".join(f32(r) for r in rows))
            q = f32([1, 0])
            self.assertEqual([i for i, _ in top(matrix, dim, q, 3)], [0, 2, 4])  # ties keep the lower row
            self.assertNotIn(1, [i for i, _ in top(matrix, dim, q, -1)])  # NaN scores are left out
            self.assertEqual(top(matrix, dim, q, 0), [])
            with self.assertRaises(IndexError):
                top(matrix, dim, q, 2, array("q", [7]))
            with self.assertRaises(IndexError):
                top(matrix, dim, q, 2, array("q", [-1]))
            with self.assertRaises(ValueError):
                top(matrix, dim, f32([1, 0, 0]), 2)

    def test_python_path_is_used_when_asked(self):
        if os.environ.get("SIQL_PURE_PYTHON") == "1":
            self.assertIs(search.vector_top, py_vector_top)
            self.assertIs(vector_module.vector_top, py_vector_top)
        elif _speedups is not None:
            self.assertIs(vector_module.vector_top, _speedups.vector_top)


class NearestTests(InTempDir):
    def table(self, rows, file="t.siql", **extra):
        t = Table(file=file)
        t.addCols(name=str, kind=str, vec=Vector)
        for i, v in enumerate(rows):
            t.add(name=f"r{i}", kind="a" if i % 2 else "b", vec=v)
        return t

    def random_rows(self, n=60, dim=16, seed=1):
        rng = random.Random(seed)
        return [[rng.uniform(-1, 1) for _ in range(dim)] for _ in range(n)]

    def test_cells_are_bytes_and_survive_reload(self):
        rows = self.random_rows(10, 8)
        t = self.table(rows)
        self.assertEqual(t.rows[3].col("vec").value, f32(rows[3]))
        self.assertIn(to_text(f32(rows[3])), read("t.siql"))
        again = Table(file="t.siql")
        self.assertIs(again.struct.columns[2], Vector)
        self.assertEqual([r.col("vec").value for r in again.rows], [f32(r) for r in rows])
        self.assertIn("<vector 8>", str(again))

    def test_top_k_and_metrics(self):
        rows = self.random_rows()
        t = self.table(rows)
        query = self.random_rows(1, seed=99)[0]
        self.assertPairs(t.nearest("vec", query, k=7), reference(rows, query, 7))
        self.assertPairs(t.nearest(2, query, k=None), reference(rows, query))
        self.assertPairs(t.nearest("vec", query, k=5, metric="dot"), reference(rows, query, 5, "dot"), places=4)
        self.assertEqual(t.nearest("vec", query, k=0), [])
        self.assertEqual(t.nearest("vec", rows[4], k=1)[0][0], 4)

    def test_filters(self):
        rows = self.random_rows()
        t = self.table(rows)
        query = rows[0]
        a_rows = {i for i in range(len(rows)) if i % 2}
        self.assertPairs(t.nearest("vec", query, 5, where={"kind": "a"}), reference(rows, query, 5, positions=a_rows))
        self.assertPairs(t.nearest("vec", query, 3, where=[2, 4, 6, 8]),
                         reference(rows, query, 3, positions={2, 4, 6, 8}))
        self.assertEqual(t.nearest("vec", query, 3, where={"kind": "none"}), [])
        with self.assertRaises(IndexError):
            t.nearest("vec", query, 3, where=[len(rows)])

    def test_rows_without_vectors(self):
        rows = self.random_rows(20)
        rows[0] = rows[5] = rows[19] = None
        t = self.table(rows)
        query = rows[3]
        self.assertPairs(t.nearest("vec", query, None), reference(rows, query))
        self.assertPairs(t.nearest("vec", query, 4, where={"kind": "a"}),
                         reference(rows, query, 4, positions={i for i in range(20) if i % 2}))
        self.assertEqual(t.nearest("vec", query, 3, where=[0, 5]), [])
        empty = self.table([None, None], file="e.siql")
        self.assertEqual(empty.nearest("vec", query, 3), [])

    def test_errors(self):
        t = self.table(self.random_rows(5, 4))
        with self.assertRaises(ValueError):
            t.nearest("vec", [1, 2, 3], 2)  # wrong length
        with self.assertRaises(ValueError):
            t.nearest("vec", [0, 0, 0, 0], 2)  # no direction for cosine
        self.assertEqual(len(t.nearest("vec", [0, 0, 0, 0], 2, metric="dot")), 2)
        with self.assertRaises(ValueError):
            t.nearest("vec", [1, 2, 3, 4], 2, metric="l2")
        with self.assertRaises(ValueError):
            t.nearest("vec", [1, 2, 3, 4], -1)
        with self.assertRaises(KeyError):
            t.nearest("nope", [1, 2, 3, 4], 2)
        with self.assertRaises(ValueError):
            t.add(name="x", vec=[1, 2, 3])  # its length is checked by the next search
            t.nearest("vec", [1, 2, 3, 4], 2)
        with self.assertRaises(TypeError):
            t.add(name="y", vec=5)

    def test_cache_follows_changes(self):
        rows = self.random_rows(30)
        t = self.table(rows)
        query = rows[10]
        self.assertEqual(t.nearest("vec", query, 1)[0][0], 10)
        TableDiff([SetCell(10, "vec", rows[11])]).apply(t)  # edit
        rows[10] = rows[11]
        self.assertPairs(t.nearest("vec", query, 3), reference(rows, query, 3))
        TableDiff([DropRow(11)]).apply(t)  # delete
        del rows[11]
        self.assertPairs(t.nearest("vec", query, 3), reference(rows, query, 3))
        t.add(name="new", vec=query)  # append extends the matrices
        rows.append(query)
        self.assertPairs(t.nearest("vec", query, 3), reference(rows, query, 3))
        self.assertPairs(t.nearest("vec", query, 3, metric="dot"), reference(rows, query, 3, "dot"), places=4)
        with self.assertRaises(Exception):  # a failed change is rolled back
            TableDiff([SetCell(0, "vec", query), SetCell(999, "vec", query)]).apply(t)
        self.assertPairs(t.nearest("vec", query, 3), reference(rows, query, 3))
        TableDiff([RenameCol("vec", "emb")]).apply(t)
        self.assertPairs(t.nearest("emb", query, 3), reference(rows, query, 3))
        with self.assertRaises(KeyError):
            t.nearest("vec", query, 3)
        TableDiff([DropCol("emb")]).apply(t)
        TableDiff([AddCol("emb", Vector, len(t.struct))]).apply(t)
        self.assertEqual(t.nearest("emb", query, 3), [])
        t.clear_cache()
        again = Table(file="t.siql")
        self.assertEqual(again.nearest("emb", query, 3), [])

    def test_compact_keeps_vectors(self):
        rows = self.random_rows(12)
        t = self.table(rows)
        TableDiff([SetCell(2, "vec", rows[3])]).apply(t)
        rows[2] = rows[3]
        t.compact()
        again = Table(file="t.siql")
        self.assertEqual([r.col("vec").value for r in again.rows], [f32(r) for r in rows])
        self.assertPairs(again.nearest("vec", rows[3], 4), reference(rows, rows[3], 4))

    def test_vector_column_added_with_rows_in_one_change(self):
        t = Table(file="t.siql")
        TableDiff([AddCol("v", Vector, 0), AddRow(0, {"v": [1, 0]}), AddRow(1, {"v": [0, 1]})]).apply(t)
        self.assertEqual(t.rows[0].col("v").value, f32([1, 0]))
        self.assertEqual(Table(file="t.siql").nearest("v", [0, 1], 1)[0][0], 1)

    def test_column_changed_to_vector(self):
        t = Table(file="t.siql")
        t.addCols(v=list)
        t.add(v=[1.0, 0.0])
        t.add(v=[0.0, 1.0])
        TableDiff([SetColType("v", Vector)]).apply(t)
        self.assertEqual(t.rows[1].col("v").value, f32([0, 1]))
        self.assertEqual(Table(file="t.siql").rows[1].col("v").value, f32([0, 1]))
        u = Table(file="u.siql")
        u.addCols(v=list)
        u.add(v=["x"])
        with self.assertRaises(TypeError):
            TableDiff([SetColType("v", Vector)]).apply(u)
        self.assertEqual((u.struct.columns[0], u.rows[0].col("v").value), (list, ["x"]))

    def test_old_files(self):
        # A file with no vectors loads as before
        with open("old.siql", "w", encoding="utf-8") as f:
            f.write("('+c', 'a', 'int', 0)\n[('+r', 0, {'a': 1}), ('+r', 1, {'a': 2})]\n")
        self.assertEqual([r.col("a").value for r in Table(file="old.siql").rows], [1, 2])
        # A vector column written by this version: an older SiQL reads the type name as `object`
        self.table([[1, 2]], file="new.siql")
        text = read("new.siql")
        self.assertIn("'Vector'", text)
        self.assertIn("'vec': 'AACAPwAAAEA='", text)  # base64 text, not a bytes literal


class QueryTests(InTempDir):
    def setUp(self):
        super().setUp()
        self.db = Interpreter(".")
        self.db.execute("CREATE TABLE assets (id int, kind str, vec vector)")
        self.db.execute("INSERT INTO assets VALUES (1, 'x', [1, 0, 0]), (2, 'x', [0.9, 0.1, 0]), "
                        "(3, 'y', [0, 1, 0]), (4, 'x', NULL), (5, 'x', [-1, 0, 0])")

    def ids(self, query):
        return [r[0] for r in self.db.execute(query).rows]

    def test_order_by_similarity(self):
        self.assertEqual(self.ids("SELECT id FROM assets ORDER BY COSINE(vec, [1, 0, 0]) DESC LIMIT 2"), [1, 2])
        self.assertEqual(self.ids("SELECT id FROM assets ORDER BY cosine(vec, [1, 0, 0])"), [1, 2, 3, 5, 4])
        self.assertEqual(self.ids("SELECT id FROM assets ORDER BY COSINE(vec, [1, 0, 0]) ASC"), [4, 5, 3, 2, 1])
        self.assertEqual(self.ids("SELECT id FROM assets WHERE kind = 'x' ORDER BY COSINE(vec, [1, 0, 0]) DESC "
                                  "LIMIT 5"), [1, 2, 5, 4])
        self.assertEqual(self.ids("SELECT id FROM assets ORDER BY COSINE(vec, [1, 0, 0]) DESC LIMIT 2 OFFSET 1"),
                         [2, 3])
        self.assertEqual(self.ids("SELECT id FROM assets ORDER BY DOT(vec, [2, 0, 0]) DESC LIMIT 1"), [1])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assets WHERE kind = 'x' "
                                         "ORDER BY COSINE(vec, [1, 0, 0]) LIMIT 2").rows, [[2]])
        self.assertEqual(self.db.execute("SELECT vec FROM assets WHERE id = 3").rows, [[f32([0, 1, 0])]])

    def test_update_import_export(self):
        self.db.execute("UPDATE assets SET vec = [0, 0, 1] WHERE id = 3")
        self.assertEqual(self.ids("SELECT id FROM assets ORDER BY COSINE(vec, [0, 0, 1]) DESC LIMIT 1"), [3])
        self.db.execute("EXPORT assets TO 'a.csv'")
        self.db.execute("IMPORT 'a.csv' INTO copy")
        self.db.execute("CREATE TABLE typed (id int, kind str, vec vector)")
        self.db.execute("IMPORT 'a.csv' INTO typed")
        self.assertEqual(self.db.execute("SELECT vec FROM typed WHERE id = 3").rows, [[f32([0, 0, 1])]])
        self.assertIn("vec", [r[0] for r in self.db.execute("DESCRIBE typed").rows])

    def test_errors(self):
        for bad in ("SELECT id FROM assets ORDER BY COSINE(vec, [1, 0]) DESC",
                    "SELECT id FROM assets ORDER BY COSINE(vec, [0, 0, 0])",
                    "SELECT id FROM assets ORDER BY COSINE(vec, [1, 0, 0]), id",
                    "SELECT id FROM assets ORDER BY COSINE(vec, 'a')",
                    "SELECT id FROM assets ORDER BY COSINE(nope, [1, 0, 0])",
                    "INSERT INTO assets VALUES (6, 'x', [1, 0, 'a'])",
                    "INSERT INTO assets VALUES (6, 'x', [])",
                    "INSERT INTO assets VALUES (6, 'x', 'abc')"):
            with self.assertRaises(QueryError, msg=bad):
                self.db.execute(bad)


if __name__ == "__main__":
    unittest.main()
