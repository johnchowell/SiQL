"""Tests for the package layout and the core (non-file) behavior of models, managers, controllers and helpers.

File saving and crash recovery are covered in test_persistence.py.
Run from the repository root with: python -m unittest discover -s src/siql/tests -t src -v
"""
import importlib
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest

from .. import models, controllers, managers, helpers
from ..models import (Table, Row, RowStruct, Cell, TableDiff, DiffOp, commit_line,
                      AddCol, DropCol, SetColType, RenameCol, AddRow, DropRow, SetCell)
from ..controllers import Command, Interpreter
from ..managers import table_file, Database
from ..helpers import ErrorHandler, type_name, type_from_name

PKG = __package__.rpartition(".")[0]
PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_PARENT = os.path.dirname(PKG_DIR)


def snapshot(t: Table):
    names = list(t.struct.column_names)
    return names, list(t.struct.columns), [[r.col(n).value for n in names] for r in t.rows]


def run_python(*args, cwd=None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=PKG_PARENT, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, *args], env=env, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)


class InTempDir(unittest.TestCase):
    """Runs each test inside a temporary working directory, since every Table writes a .siql file."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def assertConsistent(self, t: Table):
        """Rows and columns must hold the very same Cell objects."""
        self.assertEqual(set(t.cols), set(t.struct.column_names))
        for name in t.struct.column_names:
            self.assertEqual(len(t.cols[name]), len(t.rows))
            for row, cell in zip(t.rows, t.cols[name]):
                self.assertIs(row.col(name), cell)


class PackageLayoutTests(unittest.TestCase):
    SUBPACKAGES = ["models", "controllers", "managers", "helpers", "server", "tests"]
    MODULES = [
        "models.row", "models.diff", "models.tree", "models.table",
        "controllers.parser", "controllers.interpreter", "controllers.shell",
        "managers.table_file", "managers.database",
        "helpers.error_handler", "helpers.types", "helpers.format",
        "server.config", "server.app", "server.service", "server.cli", "demo",
    ]

    def test_every_folder_is_a_package(self):
        for sub in self.SUBPACKAGES:
            with self.subTest(sub=sub):
                self.assertTrue(os.path.isfile(os.path.join(PKG_DIR, sub, "__init__.py")))

    def test_old_top_level_modules_are_gone(self):
        for old in ["table.py", "table_file.py", "interpreter.py", "error_handler.py", "tests.py"]:
            with self.subTest(old=old):
                self.assertFalse(os.path.exists(os.path.join(PKG_DIR, old)))

    def test_all_exports_resolve(self):
        root = importlib.import_module(PKG)
        for module in (root, models, controllers, managers, helpers):
            for name in module.__all__:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(hasattr(module, name))

    def test_root_reexports_are_the_same_objects(self):
        root = importlib.import_module(PKG)
        self.assertEqual(sorted(root.__all__),
                         sorted(["Table", "Row", "RowStruct", "Cell", "TableDiff", "table_file", "Database",
                                 "ErrorHandler", "Command", "Interpreter", "QueryError"]))
        self.assertIs(root.Table, Table)
        self.assertIs(root.Row, Row)
        self.assertIs(root.RowStruct, RowStruct)
        self.assertIs(root.Cell, Cell)
        self.assertIs(root.TableDiff, TableDiff)
        self.assertIs(root.Command, Command)
        self.assertIs(root.Interpreter, Interpreter)
        self.assertIs(root.table_file, table_file)
        self.assertIs(root.Database, Database)
        self.assertIs(root.ErrorHandler, ErrorHandler)
        with self.assertRaises(AttributeError):
            root.missing

    def test_optional_parts_are_not_imported_with_the_core(self):
        optional = (f"{PKG}.controllers", f"{PKG}.server")
        check = f"import sys, {PKG}; print([m for m in sys.modules if m.startswith({optional!r})])"
        result = run_python("-c", check)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")
        # The interpreter loads on first use; the server only when imported
        result = run_python("-c", f"import sys, {PKG}; {PKG}.Interpreter; "
                                  f"print('{PKG}.controllers' in sys.modules, '{PKG}.server' in sys.modules)")
        self.assertEqual(result.stdout.strip(), "True False", result.stderr)

    def test_classes_live_in_their_layer(self):
        expected = {
            Table: "models.table", Row: "models.row", RowStruct: "models.row", Cell: "models.row",
            TableDiff: "models.diff", DiffOp: "models.diff",
            Command: "controllers.interpreter", Interpreter: "controllers.interpreter",
            table_file: "managers.table_file", Database: "managers.database", ErrorHandler: "helpers.error_handler",
        }
        for cls, module in expected.items():
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls.__module__, f"{PKG}.{module}")

    def test_layers_share_one_table_class(self):
        interpreter = importlib.import_module(f"{PKG}.controllers.interpreter")
        manager = importlib.import_module(f"{PKG}.managers.table_file")
        self.assertIs(interpreter.Table, Table)
        self.assertIs(manager.Table, Table)

    def test_each_module_imports_first_in_a_fresh_interpreter(self):
        # Catches circular imports that only show up depending on which module is imported first
        for module in [""] + self.MODULES + [f"{s}" for s in self.SUBPACKAGES]:
            name = f"{PKG}.{module}".rstrip(".")
            with self.subTest(module=name):
                result = run_python("-c", f"import {name}")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_dash_m_runs_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_python("-m", PKG, "-d", tmp, "CREATE TABLE t (x int); INSERT INTO t VALUES (7)")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_python("-m", PKG, "-d", tmp, "SELECT", "*", "FROM", "t")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("│ 7 │", result.stdout)
            result = run_python("-m", PKG, "-d", tmp, "SELECT * FROM missing")
            self.assertEqual(result.returncode, 1)
            self.assertIn("No such table", result.stderr)

    def test_demo_runs_with_unchanged_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_python("-m", f"{PKG}.demo", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(len([f for f in os.listdir(tmp) if f.endswith(".siql")]), 1)
        self.assertEqual(result.stdout, textwrap.dedent("""\
            ['John Doe', '123 Place St.', '(592) 010-2345', 'email@placeholder.org']
            ['Jimmy Doe', '123 Place St. (Basement)', None, 'email@placeholder.org']
            ┌───────────┬──────────────────────────┬────────────────┬───────────────────────┐
            │ name: str │ address: str             │ phone: str     │ email: str            │
            ├───────────┼──────────────────────────┼────────────────┼───────────────────────┤
            │ John Doe  │ 123 Place St.            │ (592) 010-2345 │ email@placeholder.org │
            ├───────────┼──────────────────────────┼────────────────┼───────────────────────┤
            │ Jimmy Doe │ 123 Place St. (Basement) │ None           │ email@placeholder.org │
            └───────────┴──────────────────────────┴────────────────┴───────────────────────┘
        """))


class RowModelTests(unittest.TestCase):
    def test_row_struct(self):
        s = RowStruct(name=str, age=int)
        self.assertEqual(len(s), 2)
        self.assertEqual(list(s), [0, 1])
        self.assertEqual(s.col("age"), 1)
        self.assertEqual(s.col(0), 0)
        with self.assertRaises(ValueError):
            s.col("missing")
        s.add(email=str)
        self.assertEqual(s.column_names, ["name", "age", "email"])
        self.assertEqual(s.columns, [str, int, str])

    def test_cell(self):
        c = Cell("x")
        self.assertEqual((c.value, str(c), repr(c)), ("x", "x", "'x'"))
        self.assertIsNone(Cell().value)
        self.assertEqual(str(Cell()), "None")

    def test_row_wraps_values_and_keeps_cells(self):
        s = RowStruct(a=str, b=int)
        shared = Cell(5)
        r = Row(s, ["x", shared])
        self.assertTrue(all(isinstance(c, Cell) for c in r.content))
        self.assertIs(r.col("b"), shared)
        self.assertIs(r.col(1), shared)
        self.assertEqual(r.col("a").value, "x")
        self.assertEqual(str(r), "['x', 5]")
        self.assertEqual([c.value for c in Row(s).content], [None, None])

    def test_row_unknown_column(self):
        r = Row(RowStruct(a=str))
        with self.assertRaises(IndexError):
            r.col("missing")
        self.assertEqual(r.append("missing", 1), "Column not found")

    def test_row_fills_columns_added_later(self):
        s = RowStruct(a=str)
        r = Row(s, ["x"])
        s.add(b=int)
        self.assertIsNone(r.col("b").value)
        r.append("b", 3)
        self.assertEqual([c.value for c in r.content], ["x", 3])


class TableModelTests(InTempDir):
    def people(self) -> Table:
        t = Table()
        t.addCols(name=str, address=str, phone=str)
        t.add(name="John Doe", address="123 Place St.", phone="(592) 010-2345")
        t.add(name="Jimmy Doe", address="123 Place St. (Basement)")
        return t

    def test_empty_table(self):
        t = Table()
        self.assertEqual((t.rows, t.cols, len(t.struct)), ([], {}, 0))
        self.assertEqual(str(t), "")
        self.assertEqual(list(t.select("x")), [])

    def test_add_and_columns(self):
        t = self.people()
        self.assertConsistent(t)
        self.assertIs(t.entries, t.rows)
        self.assertEqual([c.value for c in t.column("name")], ["John Doe", "Jimmy Doe"])
        self.assertEqual([c.value for c in t.column(2)], ["(592) 010-2345", None])

    def test_add_ignores_unknown_columns(self):
        t = self.people()
        t.add(name="Jane", nickname="J")
        self.assertNotIn("nickname", t.cols)
        self.assertEqual(snapshot(t)[2][-1], ["Jane", None, None])

    def test_addcols_extends_existing_rows(self):
        t = self.people()
        t.addCols(email=str, age=int)
        self.assertConsistent(t)
        self.assertEqual(t.struct.column_names, ["name", "address", "phone", "email", "age"])
        self.assertEqual([r.col("email").value for r in t.rows], [None, None])
        with self.assertRaises(ValueError):
            t.addCols(name=str)

    def test_rows_and_columns_share_cells(self):
        t = self.people()
        t.rows[0].col("name").value = "Changed"
        self.assertEqual(t.cols["name"][0].value, "Changed")
        t.cols["phone"][1].value = "555"
        self.assertEqual(t.rows[1].col("phone").value, "555")

    def test_edit_by_name(self):
        t = self.people()
        t.addCols(email=str)
        t.editByName("email", None, "e@x.org")
        self.assertEqual([c.value for c in t.column("email")], ["e@x.org", "e@x.org"])
        t.editByName("name", "Jimmy Doe", "James")
        self.assertEqual([c.value for c in t.column("name")], ["John Doe", "James"])
        t.editByName("name", "Nobody", "x")
        self.assertEqual([c.value for c in t.column("name")], ["John Doe", "James"])
        self.assertConsistent(t)

    def test_select(self):
        t = self.people()
        t.addCols(age=int)
        t.editByName("age", None, 40)
        self.assertEqual([r.col("name").value for r in t.select("John Doe")], ["John Doe"])
        self.assertEqual([r.col("name").value for r in t.select("Jimmy Doe", "name")], ["Jimmy Doe"])
        self.assertEqual([r.col("name").value for r in t.select(40)], ["John Doe", "Jimmy Doe"])
        self.assertEqual([r.col("name").value for r in t.select(None, "phone")], ["Jimmy Doe"])
        self.assertEqual(list(t.select("John Doe", "address")), [])
        self.assertIs(next(t.select("Jimmy Doe", "name")), t.rows[1])

    def test_str_table(self):
        t = Table()
        t.addCols(a=str, n=int)
        t.add(a="x", n=1)
        t.add(a="yy")
        self.assertEqual(str(t), "\n".join([
            "┌────────┬────────┐",
            "│ a: str │ n: int │",
            "├────────┼────────┤",
            "│ x      │ 1      │",
            "├────────┼────────┤",
            "│ yy     │ None   │",
            "└────────┴────────┘",
        ]))

    def test_initial_rows_keep_their_objects(self):
        s = RowStruct(a=str, b=int)
        rows = [Row(s, ["q", 1]), Row(s, ["w", 2])]
        t = Table(rows)
        self.assertIs(t.rows[0], rows[0])
        self.assertIs(t.struct, s)
        self.assertConsistent(t)
        t.editByName("a", "w", "e")
        self.assertEqual(rows[1].col("a").value, "e")


class DiffModelTests(InTempDir):
    def rand_table(self, rng: random.Random) -> Table:
        t = Table()
        cols = rng.sample(["a", "b", "c", "d", "e"], rng.randint(0, 4))
        t.addCols(**{c: rng.choice([str, int, object]) for c in cols})
        for _ in range(rng.randint(0, 6)):
            t.add(**{c: rng.choice([None, 1, 2, "x", [1], {"k": 2}]) for c in cols if rng.random() < .8})
        return t

    def test_identical_tables_have_no_diff(self):
        a, b = Table(), Table()
        for t in (a, b):
            t.addCols(x=int)
            t.add(x=1)
        d = TableDiff.between(a, b)
        self.assertFalse(d)
        self.assertEqual((len(d), list(d), d.dumps()), (0, [], ""))

    def test_between_produces_expected_ops(self):
        old, new = Table(), Table()
        old.addCols(name=str, gone=int)
        new.addCols(name=str, email=str)
        for n in ["a", "b", "c"]:
            old.add(name=n)
        for n in ["a", "c"]:
            new.add(name=n)
        self.assertEqual(TableDiff.between(old, new).ops, [
            DropCol("gone"), AddCol("email", str, 1), DropRow(1),
        ])
        new.editByName("name", "c", "z")
        self.assertEqual(TableDiff.between(old, new).ops, [
            DropCol("gone"), AddCol("email", str, 1), SetCell(1, "name", "z"), DropRow(2),
        ])

    def test_random_diffs_round_trip_and_apply(self):
        rng = random.Random(5)
        for _ in range(150):
            old, new = self.rand_table(rng), self.rand_table(rng)
            d = TableDiff.between(old, new)
            self.assertEqual(TableDiff.loads(d.dumps()), d)
            self.assertEqual(TableDiff.loads(commit_line(d.ops) if d else ""), d)
            d.apply(old)
            self.assertConsistent(old)
            on, nn = snapshot(old), snapshot(new)
            self.assertEqual(dict(zip(on[0], on[1])), dict(zip(nn[0], nn[1])))
            self.assertEqual([dict(zip(on[0], r)) for r in on[2]], [dict(zip(nn[0], r)) for r in nn[2]])
            self.assertFalse(TableDiff.between(old, new) and on[0] == nn[0])

    def test_op_text_forms(self):
        ops = [AddCol("a", int, 0), DropCol("a"), SetColType("a", str), AddRow(0, {"a": 1}), DropRow(2), SetCell(0, "a", None)]
        self.assertEqual([op.to_tuple() for op in ops], [
            ("+c", "a", "int", 0), ("-c", "a"), ("=c", "a", "str"), ("+r", 0, {"a": 1}), ("-r", 2), ("~", 0, "a", None),
        ])
        self.assertEqual(repr(SetCell(0, "a", None)), "SetCell(0, 'a', None)")
        self.assertEqual(commit_line(ops[:1]), "('+c', 'a', 'int', 0)\n")
        self.assertEqual(commit_line(ops[3:5]), "[('+r', 0, {'a': 1}), ('-r', 2)]\n")
        self.assertEqual(TableDiff.loads(TableDiff(ops).dumps()).ops, ops)
        self.assertEqual(TableDiff.loads(commit_line(ops)).ops, ops)
        self.assertNotEqual(SetCell(0, "a", 1), SetCell(0, "a", 2))
        with self.assertRaises(TypeError):
            commit_line([AddRow(0, {"a": 1}), SetCell(0, "a", object())])

    def test_rename_col(self):
        op = RenameCol("a", "b")
        self.assertEqual(op.to_tuple(), (">c", "a", "b"))
        self.assertEqual(TableDiff.loads(op.dumps()).ops, [op])
        t = Table()
        t.addCols(a=int, c=str)
        t.add(a=1, c="x")
        TableDiff([op]).apply(t)
        self.assertEqual(list(t.cols), ["b", "c"])
        self.assertConsistent(t)
        self.assertEqual(snapshot(Table(file=t._file)), (["b", "c"], [int, str], [[1, "x"]]))
        with self.assertRaises(ValueError):
            TableDiff([RenameCol("b", "c")]).apply(t)
        self.assertEqual(list(t.cols), ["b", "c"])

    def test_compact(self):
        t = Table()
        t.addCols(a=int)
        for i in range(20):
            t.add(a=i)
        t.editByName("a", 3, 99)
        TableDiff([DropRow(0), DropRow(0)]).apply(t)
        before = os.path.getsize(t._file)
        t.compact()
        self.assertLess(os.path.getsize(t._file), before)
        self.assertFalse(os.path.exists(t._file + ".tmp"))
        self.assertEqual(snapshot(Table(file=t._file)), snapshot(t))
        t.add(a=100)
        self.assertEqual(snapshot(Table(file=t._file)), snapshot(t))

    def test_write(self):
        import io
        buf = io.StringIO()
        TableDiff([DropRow(0)]).write(buf)
        self.assertEqual(buf.getvalue(), "('-r', 0)\n")


class ManagerTests(InTempDir):
    def test_table_file_diff_and_sync(self):
        tf = table_file()
        self.assertIsInstance(tf.table, Table)
        t = Table()
        t.addCols(x=int, y=str)
        t.add(x=1, y="a")
        d = tf.diff(t)
        self.assertIsInstance(d, TableDiff)
        self.assertTrue(d)
        d.apply(tf.table)
        self.assertFalse(tf.diff(t))
        self.assertEqual(snapshot(tf.table), snapshot(t))
        self.assertEqual(snapshot(Table(file=tf.table._file)), snapshot(t))


class ControllerTests(unittest.TestCase):
    def test_str_to_op(self):
        cases = {"<": (1, 2, True), ">": (1, 2, False), ">=": (2, 2, True),
                 "<=": (3, 2, False), "==": (2, 2, True), "!=": (2, 2, False)}
        for op, (l, r, expected) in cases.items():
            with self.subTest(op=op):
                self.assertIs(Command.strToOp(op)(l, r), expected)

    def test_from_str(self):
        self.assertEqual(Command.fromStr("  SELECT "), 0)
        self.assertEqual(Command.fromStr("where"), 1)
        self.assertEqual(
            [Command.fromStr(s) for s in ["equals", "from", "and", "or", "if"]], [2, 3, 4, 5, 6])
        self.assertIsNone(Command.fromStr("unknown"))


class HelperTests(unittest.TestCase):
    def test_type_names_round_trip(self):
        for t in (str, int, float, bool, bytes, list, dict, tuple, set, object, type(None)):
            with self.subTest(t=t):
                self.assertIs(type_from_name(type_name(t)), t)

    def test_unknown_types_become_object(self):
        class Custom: pass
        self.assertEqual(type_name(Custom), "Custom")
        self.assertIs(type_from_name("Custom"), object)
        self.assertIs(type_from_name("print"), object)  # a builtin, but not a type

    def test_error_handler_is_importable(self):
        self.assertTrue(isinstance(ErrorHandler, type))


if __name__ == "__main__":
    unittest.main()
