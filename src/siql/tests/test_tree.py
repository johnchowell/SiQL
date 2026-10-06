"""Tests for the per-table search tree (column name -> first character/digit -> cells)."""
import os
import random
import tempfile
import unittest
from unittest import mock

from ..models import Table, Row, RowStruct, TableDiff, AddCol, DropCol, RenameCol, AddRow, DropRow, SetCell, tree_key
from ..controllers import Interpreter


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def assertTreeMatches(self, t: Table):
        """The tree holds exactly the table's own Row and Cell objects, each in the branch for its value,
        plus (with lazy deletes) queued dead cells that aren't in the table any more."""
        self.assertEqual(set(t.index), set(t.struct.column_names))
        for name in t.struct.column_names:
            node = t.index[name]
            dead = node.dead
            expected = {}
            for row, cell in zip(t.rows, t.cols[name]):
                self.assertIs(row.col(name), cell)
                self.assertIs(cell._tree, node)
                self.assertNotIn(id(cell), dead)
                expected.setdefault(tree_key(cell.value), {})[id(cell)] = (row, cell)
            live = {k: {i: e for i, e in b.items() if i not in dead} for k, b in node.branches.items()}
            live = {k: b for k, b in live.items() if b}
            self.assertEqual(live.keys(), expected.keys(), name)
            for key, branch in expected.items():
                self.assertEqual(live[key].keys(), branch.keys())
                for k, (row, cell) in branch.items():
                    self.assertIs(live[key][k][0], row)
                    self.assertIs(live[key][k][1], cell)
            # Each queued cell is filed under the branch it's actually in
            for key, queued in node._dead.items():
                self.assertTrue(queued)
                for cell_id in queued:
                    self.assertIn(cell_id, node.branches[key])
            self.assertEqual(node._dead_count, len(dead))
            self.assertEqual(len(node), len(t.rows))

    def scan(self, t: Table, col, value):
        return [i for i, c in enumerate(t.cols[col]) if value == c.value]


class TreeKeyTests(unittest.TestCase):
    def test_keys(self):
        cases = [("John", "J"), ("j", "j"), ("", ""), ("42abc", "4"), (42, "4"), (-7, "7"), (0, "0"), (3.9, "3"),
                 (-0.5, "0"), (1.0, "1"), (True, "1"), (False, "0"), (10**30, "1"), (None, None),
                 (float("nan"), None), (float("inf"), None), ((1, 2), None), ([1], None)]
        for value, key in cases:
            with self.subTest(value=value):
                self.assertEqual(tree_key(value), key)

    def test_equal_values_share_a_key(self):
        for a, b in [(1, 1.0), (1, True), (0, False), (0, -0.0), (10**20, 1e20), (-3, -3.0)]:
            with self.subTest(a=a, b=b):
                self.assertEqual(a, b)
                self.assertEqual(tree_key(a), tree_key(b))


class TreeTests(InTempDir):
    def make(self, **kw):
        t = Table(**kw)
        t.addCols(name=str, n=int)
        t.add(name="John", n=42)
        t.add(name="Jane", n=7)
        t.add(name="amy", n=45)
        return t

    def test_on_by_default_and_layered(self):
        t = self.make()
        self.assertTrue(t.tree)
        self.assertEqual(list(t.index), ["name", "n"])
        self.assertEqual(sorted(t.index["name"].branches), ["J", "a"])
        self.assertEqual(sorted(t.index["n"].branches), ["4", "7"])
        self.assertEqual(t.index["name"]["J"], [t.rows[0], t.rows[1]])
        self.assertEqual(t.index["n"]["4"], [t.rows[0], t.rows[2]])
        self.assertEqual(len(t.index["n"]), 3)
        self.assertTreeMatches(t)

    def test_references_are_the_real_rows_and_cells(self):
        t = self.make()
        (row, cell), = t.index["name"].branches["a"].values()
        self.assertIs(row, t.rows[2])
        self.assertIs(cell, t.cols["name"][2])
        self.assertIs(cell, row.content[0])

    def test_find_and_select(self):
        t = self.make()
        self.assertEqual(t.find("name", "Jane"), [1])
        self.assertEqual(t.find("n", 45), [2])
        self.assertEqual(t.find(1, 45.0), [2])
        self.assertEqual(t.find("name", "J"), [])
        self.assertEqual([r.col("name").value for r in t.select("John")], ["John"])
        self.assertEqual([r.col("name").value for r in t.select(7, "n")], ["Jane"])
        with mock.patch.object(type(t.index["n"]), "find", wraps=t.index["n"].find) as find:
            list(t.select(42, "n"))
            find.assert_called_once()

    def test_kept_current_through_every_change(self):
        t = self.make()
        t.editByName("name", "John", "Zed")
        self.assertEqual(t.find("name", "Zed"), [0])
        self.assertNotIn("John", [c.value for _, c in t.index["name"].branches["J"].values()])
        t.cols["n"][1].value = 900  # direct assignment, bypassing the table
        self.assertEqual(t.find("n", 900), [1])
        t.rows[2].append("name", "Bea")
        self.assertEqual(t.find("name", "Bea"), [2])
        TableDiff([AddRow(0, {"name": "first", "n": 1})]).apply(t)
        TableDiff([DropRow(2)]).apply(t)
        t.addCols(email=str)
        t.editByName("email", None, "x@y")
        TableDiff([RenameCol("n", "num")]).apply(t)
        TableDiff([DropCol("email")]).apply(t)
        self.assertTreeMatches(t)
        self.assertEqual(t.find("num", 1), [0])
        self.assertEqual(t.find("name", "Bea"), [2])

    def test_failed_changes_leave_the_tree_as_it_was(self):
        t = self.make()
        with self.assertRaises(Exception):
            TableDiff([AddRow(0, {"name": "x"}), DropRow(1), AddCol("c", str, 2), RenameCol("name", "nm"),
                       DropCol("n"), SetCell(0, "name", "y"), SetCell(0, "missing", 1)]).apply(t)
        self.assertTreeMatches(t)
        self.assertEqual([r.col("name").value for r in t.rows], ["John", "Jane", "amy"])
        self.assertEqual(t.find("n", 7), [1])

    def test_unusual_values(self):
        t = self.make()
        class AlwaysEqual:
            def __eq__(self, other):
                return True
            __hash__ = object.__hash__
        nan, odd = float("nan"), AlwaysEqual()
        t.cols["n"][0].value = nan
        t.cols["n"][1].value = odd
        t.cols["n"][2].value = None
        self.assertTreeMatches(t)
        for probe in [nan, 7, None, "x", odd, 1.0]:
            with self.subTest(probe=probe):
                self.assertEqual(t.find("n", probe), self.scan(t, "n", probe))

    def test_turning_the_tree_off_and_on(self):
        t = self.make(tree=False)
        self.assertFalse(t.tree)
        self.assertIsNone(t.index)
        self.assertEqual(t.find("name", "Jane"), [1])
        t.tree = True
        self.assertTreeMatches(t)
        t.tree = False
        self.assertIsNone(t.index)
        self.assertTrue(all(c._tree is None for cells in t.cols.values() for c in cells))
        t.add(name="Kim", n=1)
        self.assertEqual(t.find("name", "Kim"), [3])

    def test_built_when_loading_and_from_initial_rows(self):
        t = self.make()
        loaded = Table(file=t._file)
        self.assertTreeMatches(loaded)
        self.assertEqual(loaded.find("name", "amy"), [2])
        self.assertIsNone(Table(file=t._file, tree=False).index)

        struct = RowStruct(a=str)
        rows = [Row(struct, ["x"]), Row(struct, ["y"])]
        built = Table(rows)
        self.assertTreeMatches(built)
        self.assertIs(built.index["a"]["y"][0], rows[1])

    def test_random_changes_match_a_full_scan(self):
        rng = random.Random(7)
        values = ["", "a", "ab", "b", "B", "1x", 0, 1, 10, -1, 1.0, 1.5, True, False, None, [1], {"k": 2}]
        t = Table()
        t.addCols(a=object, b=object)
        for step in range(400):
            op = rng.random()
            if op < 0.35 or not t.rows:
                t.add(a=rng.choice(values), b=rng.choice(values))
            elif op < 0.5:
                TableDiff([DropRow(rng.randrange(len(t.rows)))]).apply(t)
            elif op < 0.8:
                t.editByName(rng.choice("ab"), rng.choice(values), rng.choice(values))
            else:
                rng.choice(t.cols[rng.choice("ab")]).value = rng.choice(values)
            if step % 20 == 0:
                self.assertTreeMatches(t)
            col, probe = rng.choice("ab"), rng.choice(values)
            self.assertEqual(t.find(col, probe), self.scan(t, col, probe))
        self.assertTreeMatches(t)


class LazyDeleteTests(InTempDir):
    def make(self, n=6, **kw):
        t = Table(lazy_delete=True, **kw)
        t.addCols(name=str, n=int)
        for i, name in enumerate(["Ann", "Bob", "Amy", "Ben", "Al", "Cy"][:n]):
            t.add(name=name, n=i)
        return t

    def test_off_by_default(self):
        t = Table()
        self.assertFalse(t.lazy_delete)
        t.addCols(a=str)
        t.add(a="x")
        TableDiff([DropRow(0)]).apply(t)
        self.assertEqual(t.index["a"].dead, set())
        self.assertEqual(t.index["a"].branches, {})

    def test_deleted_rows_are_queued_then_unlinked_by_searches(self):
        t = self.make()
        t.find("name", "Ann")  # build the position map
        ann, bob = t.rows[0], t.rows[1]
        TableDiff([DropRow(0)]).apply(t)
        TableDiff([DropRow(0)]).apply(t)
        # The table itself changes at once
        self.assertEqual([r.col("name").value for r in t.rows], ["Amy", "Ben", "Al", "Cy"])
        self.assertEqual(t._deleted, [0, 1])
        node = t.index["name"]
        self.assertEqual(node.dead, {id(ann.col("name")), id(bob.col("name"))})
        self.assertEqual(len(node), 4)
        self.assertTreeMatches(t)

        # A search through the "A" branch unlinks Ann but leaves Bob queued in the "B" branch
        self.assertEqual(t.find("name", "Al"), [2])
        self.assertEqual(node.dead, {id(bob.col("name"))})
        self.assertIsNone(ann.col("name")._tree)
        self.assertEqual(node["B"], [t.rows[1]])
        self.assertEqual(node.dead, set())
        self.assertTreeMatches(t)

    def test_searches_only_clean_the_branches_they_read(self):
        t = self.make()
        node = t.index["name"]
        TableDiff([DropRow(1)]).apply(t)  # Bob, in branch "B"
        TableDiff([DropRow(0)]).apply(t)  # Ann, in branch "A"
        self.assertEqual(set(node._dead), {"A", "B"})
        t.find("name", "Cy")
        self.assertEqual(set(node._dead), {"A", "B"})  # branch "C" had nothing queued
        t.find("name", "Ben")
        self.assertEqual(set(node._dead), {"A"})
        self.assertTreeMatches(t)

    def test_changing_a_deleted_rows_cell_keeps_it_queued_in_the_right_branch(self):
        t = self.make()
        gone = t.rows[0]
        TableDiff([DropRow(0)]).apply(t)
        gone.col("name").value = "Zed"  # still watched until a search unlinks it
        node = t.index["name"]
        self.assertEqual(node._dead, {"Z": {id(gone.col("name"))}})
        self.assertTreeMatches(t)
        self.assertEqual(t.find("name", "Zed"), [])
        self.assertEqual(node._dead, {})
        self.assertIsNone(gone.col("name")._tree)

    def test_without_the_tree_it_changes_nothing(self):
        t = self.make(tree=False)
        self.assertTrue(t.lazy_delete)
        TableDiff([DropRow(1), DropRow(3)]).apply(t)  # Bob, then Al
        self.assertEqual([r.col("name").value for r in t.rows], ["Ann", "Amy", "Ben", "Cy"])
        self.assertEqual(t.find("name", "Cy"), [3])
        self.assertEqual(t._deleted, [])
        t.tree = True  # built from the current rows, nothing queued
        self.assertTreeMatches(t)
        self.assertEqual(t.index["name"].dead, set())
        TableDiff([DropRow(0)]).apply(t)
        self.assertEqual(t.find("name", "Cy"), [2])
        self.assertTreeMatches(t)

    def test_positions_account_for_the_queue(self):
        t = self.make()
        t.find("n", 0)
        TableDiff([DropRow(1)]).apply(t)  # Bob
        TableDiff([DropRow(2)]).apply(t)  # Ben
        t.add(name="Dee", n=9)
        self.assertEqual(t.find("name", "Al"), [2])
        self.assertEqual(t.find("name", "Cy"), [3])
        self.assertEqual(t.find("name", "Dee"), [4])
        self.assertEqual(t.find("n", 0), [0])
        self.assertEqual([r.col("name").value for r in t.select("Amy")], ["Amy"])

    def test_failed_commit_restores_lazily_deleted_rows(self):
        t = self.make()
        t.find("n", 0)
        with self.assertRaises(Exception):
            TableDiff([DropRow(0), DropRow(5 - 1), SetCell(0, "missing", 1)]).apply(t)
        self.assertTreeMatches(t)
        self.assertEqual([t.find("n", i) for i in range(6)], [[i] for i in range(6)])

    def test_purge_and_turning_the_tree_off(self):
        t = self.make()
        gone = t.rows[0]
        TableDiff([DropRow(0)]).apply(t)
        t.purge()
        self.assertTrue(all(not node.dead for node in t.index.values()))
        self.assertTrue(all(c._tree is None for c in gone.content))
        self.assertTreeMatches(t)

        TableDiff([DropRow(0)]).apply(t)
        t.tree = False
        self.assertIsNone(t.index)
        self.assertEqual(t.find("name", "Amy"), [0])

    def test_queue_stays_bounded(self):
        t = Table(lazy_delete=True)
        t.addCols(a=int)
        TableDiff([AddRow(i, {"a": i}) for i in range(1000)]).apply(t)
        t.find("a", 0)
        for _ in range(900):
            TableDiff([DropRow(len(t.rows) // 2)]).apply(t)
            self.assertLessEqual(len(t.index["a"].dead), max(64, len(t.rows)) + 1)
            self.assertLessEqual(len(t._deleted), len(t.rows) // 4 + 64)
        self.assertTreeMatches(t)
        self.assertEqual([t.find("a", r.col("a").value) for r in t.rows], [[i] for i in range(len(t.rows))])

    def test_saved_file_is_the_same(self):
        t = self.make()
        TableDiff([DropRow(1)]).apply(t)
        loaded = Table(file=t._file)
        self.assertEqual([r.col("name").value for r in loaded.rows], ["Ann", "Amy", "Ben", "Al", "Cy"])

    def test_random_changes_match_a_full_scan(self):
        rng = random.Random(11)
        values = ["", "a", "ab", "b", "1x", 0, 1, 10, 1.5, True, None]
        t = Table(lazy_delete=True)
        t.addCols(a=object, b=object)
        for step in range(600):
            op = rng.random()
            if op < 0.35 or not t.rows:
                TableDiff([AddRow(rng.randrange(len(t.rows) + 1), {"a": rng.choice(values), "b": rng.choice(values)})]).apply(t)
            elif op < 0.6:
                TableDiff([DropRow(rng.randrange(len(t.rows)))]).apply(t)
            elif op < 0.7:
                t.add(a=rng.choice(values), b=rng.choice(values))
            elif op < 0.85:
                t.editByName(rng.choice("ab"), rng.choice(values), rng.choice(values))
            else:
                rng.choice(t.cols[rng.choice("ab")]).value = rng.choice(values)
            if step % 25 == 0:
                self.assertTreeMatches(t)
            col, probe = rng.choice("ab"), rng.choice(values)
            self.assertEqual(t.find(col, probe), self.scan(t, col, probe))
        self.assertTreeMatches(t)


class InterpreterTreeTests(InTempDir):
    def test_where_equality_uses_the_tree(self):
        db = Interpreter("data")
        db.executescript("CREATE TABLE p (name str, n int); "
                         "INSERT INTO p VALUES ('Ann', 1), ('Bob', 2), ('Al', 1), ('Ann', 3)")
        queries = ["SELECT * FROM p WHERE name = 'Ann'", "SELECT * FROM p WHERE 'Ann' == name AND n > 1",
                   "SELECT * FROM p WHERE n = 1.0 ORDER BY name", "SELECT * FROM p WHERE name = 'Zed'"]
        with mock.patch.object(Table, "find", autospec=True, side_effect=Table.find) as find:
            with_tree = [db.execute(q).rows for q in queries]
        self.assertEqual(find.call_count, len(queries))
        db.database.table("p").tree = False
        self.assertEqual([db.execute(q).rows for q in queries], with_tree)
        self.assertEqual(with_tree[1], [["Ann", 3]])
        db.database.table("p").tree = True
        db.execute("UPDATE p SET name = 'Bo' WHERE name = 'Bob'")
        db.execute("DELETE FROM p WHERE n = 1")
        self.assertEqual(db.execute("SELECT name FROM p WHERE name = 'Bo'").rows, [["Bo"]])
        self.assertEqual(db.execute("SELECT name FROM p").rows, [["Bo"], ["Ann"]])


if __name__ == "__main__":
    unittest.main()
