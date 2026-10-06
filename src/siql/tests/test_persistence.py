"""Tests for Table file persistence and crash recovery.

Run from the repository root with: python -m unittest discover -s tests -t .. -v
"""
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest

from ..models import Table, Row, RowStruct, TableDiff, AddCol, DropCol, SetColType, AddRow, DropRow, SetCell

# Name of the package under test and the folder containing it, so child processes can import it too
PKG = __package__.rpartition(".")[0]
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def snapshot(t: Table):
    """Comparable view of a table: column names, column types and row values."""
    names = list(t.struct.column_names)
    return names, list(t.struct.columns), [[r.col(n).value for n in names] for r in t.rows]


def read(path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class FileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "table.siql")

    def tearDown(self):
        self._tmp.cleanup()

    def assertConsistent(self, t: Table):
        """Rows and columns must hold the very same Cell objects."""
        self.assertEqual(set(t.cols), set(t.struct.column_names))
        for name in t.struct.column_names:
            self.assertEqual(len(t.cols[name]), len(t.rows))
            for row, cell in zip(t.rows, t.cols[name]):
                self.assertIs(row.col(name), cell)

    def assertReloadsTo(self, t: Table, path=None) -> Table:
        """Open the file in a fresh Table and check it matches `t`."""
        loaded = Table(file=path or self.path)
        self.assertConsistent(loaded)
        self.assertEqual(snapshot(loaded), snapshot(t))
        return loaded

    def run_child(self, code: str, *args, **kwargs) -> subprocess.Popen:
        """Start a separate Python process that can import the package. `PKG` in `code` is its name."""
        env = dict(os.environ, PYTHONPATH=PKG_PARENT, PYTHONIOENCODING="utf-8")
        return subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(code).replace("PKG", PKG), *args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", **kwargs,
        )

    def build_people(self, path=None) -> Table:
        t = Table(file=path or self.path)
        t.addCols(name=str, address=str, phone=str)
        t.add(name="John Doe", address="123 Place St.", phone="(592) 010-2345")
        t.add(name="Jimmy Doe", address="123 Place St. (Basement)")
        t.addCols(email=str)
        t.editByName("email", None, "email@placeholder.org")
        return t


class SaveTests(FileTestCase):
    def test_new_table_creates_empty_file(self):
        t = Table(file=self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(read(self.path), "")
        self.assertEqual(snapshot(t), ([], [], []))

    def test_each_update_is_written_immediately(self):
        t = Table(file=self.path)
        sizes = [os.path.getsize(self.path)]
        for update in (lambda: t.addCols(name=str, age=int),
                       lambda: t.add(name="Ann", age=30),
                       lambda: t.editByName("age", 30, 31)):
            update()
            self.assertEqual(t._buffer.getvalue(), "", "buffer should be flushed after every update")
            sizes.append(os.path.getsize(self.path))
            self.assertReloadsTo(t)
        self.assertEqual(sizes, sorted(set(sizes)), "file should grow on every update")

    def test_file_contents_are_the_logged_ops(self):
        t = Table(file=self.path)
        t.addCols(name=str)
        t.add(name="Ann")
        t.editByName("name", "Ann", "Bob")
        self.assertEqual(read(self.path), "('+c', 'name', 'str', 0)\n('+r', 0, {'name': 'Ann'})\n('~', 0, 'name', 'Bob')\n")

    def test_multi_op_change_is_written_as_one_line(self):
        t = Table(file=self.path)
        t.addCols(name=str, age=int)
        t.add(name="Ann")
        t.add(name="Bob")
        t.editByName("age", None, 1)
        self.assertEqual(read(self.path).splitlines(), [
            "[('+c', 'name', 'str', 0), ('+c', 'age', 'int', 1)]",
            "('+r', 0, {'name': 'Ann'})",
            "('+r', 1, {'name': 'Bob'})",
            "[('~', 0, 'age', 1), ('~', 1, 'age', 1)]",
        ])
        self.assertReloadsTo(t)

    def test_edit_with_no_matches_writes_nothing(self):
        t = self.build_people()
        before = read(self.path)
        t.editByName("name", "Nobody", "x")
        self.assertEqual(read(self.path), before)

    def test_reload_restores_table(self):
        t = self.build_people()
        loaded = self.assertReloadsTo(t)
        self.assertEqual(str(loaded), str(t))
        self.assertEqual([r.col("name").value for r in loaded.select("Jimmy Doe", "name")], ["Jimmy Doe"])

    def test_reload_then_continue_editing(self):
        t = self.build_people()
        loaded = Table(file=self.path)
        loaded.add(name="Jane Doe", email="jane@x.org")
        loaded.editByName("address", None, "unknown")
        before = os.path.getsize(self.path)
        self.assertReloadsTo(loaded)
        # Loading on its own must not append anything
        Table(file=self.path)
        self.assertEqual(os.path.getsize(self.path), before)
        self.assertNotEqual(snapshot(loaded), snapshot(t))

    def test_tables_on_different_files_are_independent(self):
        a = Table(file=os.path.join(self.dir, "a.siql"))
        b = Table(file=os.path.join(self.dir, "b.siql"))
        a.addCols(x=int)
        b.addCols(y=str)
        a.add(x=1)
        self.assertReloadsTo(a, a._file)
        self.assertReloadsTo(b, b._file)

    def test_default_file_names_are_unique(self):
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            files = {Table()._file for _ in range(20)}
        finally:
            os.chdir(cwd)
        self.assertEqual(len(files), 20)
        self.assertTrue(all(os.path.exists(os.path.join(self.dir, f)) for f in files))

    def test_initial_rows_are_saved(self):
        s = RowStruct(name=str, n=int)
        rows = [Row(s, ["a", 1]), Row(s, ["b", 2])]
        t = Table(rows, file=self.path)
        self.assertIs(t.rows[0], rows[0])
        self.assertReloadsTo(t)

    def test_initial_rows_rejected_for_existing_file(self):
        self.build_people()
        before = read(self.path)
        with self.assertRaises(ValueError):
            Table([Row(RowStruct(x=int), [1])], file=self.path)
        self.assertEqual(read(self.path), before)

    def test_value_types_round_trip(self):
        t = Table(file=self.path)
        t.addCols(v=object)
        values = [None, True, 0, -12, 3.5, "text", "", "line\nbreak", "quote ' \"", "日本 ─ é 🙂",
                  [1, [2]], (1, "a"), {"k": [1, None]}, {1, 2}, b"bytes"]
        for v in values:
            t.add(v=v)
        loaded = self.assertReloadsTo(t)
        self.assertEqual([r.col("v").value for r in loaded.rows], values)

    def test_column_types_round_trip(self):
        class Custom: pass
        t = Table(file=self.path)
        t.addCols(s=str, i=int, f=float, b=bool, o=object, c=Custom)
        self.assertEqual(Table(file=self.path).struct.columns, [str, int, float, bool, object, object])

    def test_unwritable_value_is_rejected_without_changes(self):
        t = self.build_people()
        before = (read(self.path), snapshot(t))
        with self.assertRaises(TypeError):
            t.add(name=object())
        with self.assertRaises(TypeError):
            TableDiff([AddRow(0, {"name": "ok"}), SetCell(0, "name", object())]).apply(t)
        self.assertEqual((read(self.path), snapshot(t)), before)

    def test_failed_batch_is_rolled_back(self):
        t = self.build_people()
        before = (read(self.path), snapshot(t))
        with self.assertRaises(ValueError):
            t.addCols(age=int, name=str)  # age would be added, then name fails as a duplicate
        self.assertNotIn("age", t.struct.column_names)
        self.assertEqual((read(self.path), snapshot(t)), before)
        self.assertConsistent(t)

    def test_rollback_undoes_every_op_type_and_keeps_objects(self):
        t = self.build_people()
        t.add(name="Third", phone="555")
        before = (read(self.path), snapshot(t))
        rows, cells = list(t.rows), {n: list(c) for n, c in t.cols.items()}
        ops = [
            SetCell(0, "name", "changed"),
            DropRow(1),
            DropCol("address"),
            SetColType("phone", int),
            AddCol("age", int, 1),
            AddRow(0, {"name": "inserted", "age": 3}),
            DropRow(-1),
            SetCell(0, "missing column", 1),  # fails, so everything above must be undone
        ]
        with self.assertRaises(IndexError):
            TableDiff(ops).apply(t)
        self.assertEqual((read(self.path), snapshot(t)), before)
        self.assertConsistent(t)
        self.assertTrue(all(a is b for a, b in zip(t.rows, rows)) and len(t.rows) == len(rows))
        for name, column in cells.items():
            self.assertTrue(all(a is b for a, b in zip(t.cols[name], column)))
        t.add(name="after rollback")
        self.assertReloadsTo(t)

    def test_applied_diff_is_saved(self):
        t = self.build_people()
        target = Table(file=os.path.join(self.dir, "target.siql"))
        target.addCols(name=str, email=str, age=int)
        target.add(name="Jimmy Doe", email="j@x.org", age=40)
        target.add(name="New Person")
        TableDiff.between(t, target).apply(t)
        self.assertEqual(snapshot(t), snapshot(target))
        self.assertReloadsTo(target)

    def test_saved_by_one_process_loaded_by_another(self):
        child = self.run_child("""
            import sys
            from PKG.models import Table
            t = Table(file=sys.argv[1])
            t.addCols(name=str, n=int)
            for i in range(25):
                t.add(name=f"row {i}", n=i)
            t.editByName("n", 7, 700)
        """, self.path)
        _, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 0, err)
        loaded = Table(file=self.path)
        self.assertEqual([r.col("n").value for r in loaded.rows], [700 if i == 7 else i for i in range(25)])
        self.assertConsistent(loaded)


class CrashRecoveryTests(FileTestCase):
    # Child that keeps adding rows and reports each one only after add() has returned (i.e. was saved)
    WRITER = """
        import sys
        from PKG.models import Table
        t = Table(file=sys.argv[1])
        if not t.struct.column_names:
            t.addCols(i=int, text=str)
        i = len(t.rows)
        while True:
            t.add(i=i, text="x" * (i % 50))
            print(i, flush=True)
            i += 1
    """

    def kill_writer_after(self, acks: int) -> int:
        """Run WRITER, hard-kill it after `acks` saved rows. Returns the last acknowledged row number."""
        child = self.run_child(self.WRITER, self.path)
        last = -1
        try:
            for _ in range(acks):
                line = child.stdout.readline()
                if not line:
                    self.fail(f"writer exited early: {child.stderr.read()}")
                last = int(line)
        finally:
            child.kill()
            child.wait(timeout=30)
            child.stdout.close()
            child.stderr.close()
        return last

    def assertRowsAreContiguous(self, t: Table, at_least: int):
        values = [r.col("i").value for r in t.rows]
        self.assertGreaterEqual(len(values), at_least, "rows reported as saved were lost")
        self.assertEqual(values, list(range(len(values))))
        self.assertEqual([r.col("text").value for r in t.rows], ["x" * (i % 50) for i in values])

    def test_hard_kill_keeps_every_saved_change(self):
        rng = random.Random(7)
        last = -1
        for _ in range(5):  # kill, recover and resume several times on the same file
            last = self.kill_writer_after(rng.randint(10, 150))
            t = Table(file=self.path)
            self.assertConsistent(t)
            self.assertRowsAreContiguous(t, last + 1)
        t.add(i=len(t.rows), text="x" * (len(t.rows) % 50))
        self.assertRowsAreContiguous(self.assertReloadsTo(t), last + 2)

    def test_kill_before_flush_loses_only_the_unsaved_change(self):
        child = self.run_child("""
            import os, sys
            from PKG.models import Table
            t = Table(file=sys.argv[1])
            t.addCols(i=int)
            for i in range(5):
                t.add(i=i)
            Table.save = lambda self: os._exit(3)  # die after the change is buffered, before it reaches the file
            t.add(i=99)
        """, self.path)
        _, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 3, err)
        t = Table(file=self.path)
        self.assertEqual([r.col("i").value for r in t.rows], [0, 1, 2, 3, 4])
        t.add(i=5)
        self.assertReloadsTo(t)

    def test_exit_with_lazy_deletes_waiting_keeps_them(self):
        # The process ends without purging its queued deletes; each was saved to the file when it was made
        child = self.run_child("""
            import os, sys
            from PKG.models import Table, TableDiff, AddRow, DropRow
            t = Table(file=sys.argv[1], lazy_delete=True)
            t.addCols(name=str, n=int)
            TableDiff([AddRow(i, {"name": f"r{i}", "n": i}) for i in range(100)]).apply(t)
            t.find("n", 0)
            for i in (90, 50, 10, 0):
                TableDiff([DropRow(i)]).apply(t)
            print(len(t.index["name"].dead), len(t._deleted), flush=True)
            os._exit(0)
        """, self.path)
        out, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(out.split(), ["4", "4"])  # all four deletes were still waiting at exit

        t = Table(file=self.path, lazy_delete=True)
        expected = [f"r{i}" for i in range(100) if i not in (0, 10, 50, 90)]
        self.assertEqual([r.col("name").value for r in t.rows], expected)
        self.assertEqual(t.index["name"].dead, set())
        self.assertEqual(t.find("name", "r50"), [])
        self.assertEqual(t.find("name", "r51"), [48])
        self.assertConsistent(t)

    def test_hard_kill_during_large_changes_keeps_them_whole(self):
        # Each change adds BATCH rows in one commit; its line is far larger than Python's write buffer,
        # so a kill can land part-way through writing it
        batch = 200
        writer = """
            import sys
            from PKG.models import Table
            from PKG.models import TableDiff, AddRow
            t = Table(file=sys.argv[1])
            if not t.struct.column_names:
                t.addCols(i=int, text=str)
            b = len(t.rows) // BATCH
            while True:
                TableDiff([AddRow(len(t.rows) + k, {"i": b, "text": "y" * 100}) for k in range(BATCH)]).apply(t)
                print(b, flush=True)
                b += 1
        """.replace("BATCH", str(batch))
        rng = random.Random(11)
        for _ in range(5):
            child = self.run_child(writer, self.path)
            last = -1
            try:
                for _ in range(rng.randint(1, 15)):
                    last = int(child.stdout.readline())
            finally:
                child.kill()
                child.wait(timeout=30)
                child.stdout.close()
                child.stderr.close()

            t = Table(file=self.path)
            self.assertConsistent(t)
            values = [r.col("i").value for r in t.rows]
            self.assertEqual(len(values) % batch, 0, "a change was only partly saved")
            self.assertGreaterEqual(len(values) // batch, last + 1, "a saved change was lost")
            self.assertEqual(values, [b for b in range(len(values) // batch) for _ in range(batch)])

    def test_kill_mid_write_recovers_from_torn_line(self):
        child = self.run_child("""
            import os, sys
            from PKG.models import Table
            t = Table(file=sys.argv[1])
            t.addCols(i=int, flag=bool)
            for i in range(20):
                t.add(i=i, flag=False)
            def torn_save(self):
                data = self._buffer.getvalue()
                with open(self._file, "a", encoding="utf-8") as f:
                    f.write(data[: len(data) // 2 + 3])  # cut off in the middle of a line
                os._exit(3)
            Table.save = torn_save
            t.editByName("flag", False, True)  # one commit with 20 cell edits
        """, self.path)
        _, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 3, err)
        with open(self.path, "rb") as f:
            self.assertFalse(f.read().endswith(b"\n"), "test setup should leave a torn last line")

        t = Table(file=self.path)
        self.assertConsistent(t)
        self.assertEqual([r.col("i").value for r in t.rows], list(range(20)))
        # The edit was one change, so none of it may survive a write that was cut off
        self.assertEqual([r.col("flag").value for r in t.rows], [False] * 20)
        with open(self.path, "rb") as f:
            self.assertTrue(f.read().endswith(b"\n"), "torn line should be truncated on load")

        t.editByName("flag", False, True)
        loaded = self.assertReloadsTo(t)
        self.assertEqual([r.col("flag").value for r in loaded.rows], [True] * 20)

    def test_every_truncation_point_recovers(self):
        full = self.build_people()
        full.add(name="日本 ─ é", phone=None)
        full.editByName("name", "John Doe", "Johnny")
        with open(self.path, "rb") as f:
            data = f.read()
        lines = data.splitlines(keepends=True)
        ends = [sum(map(len, lines[:k + 1])) for k in range(len(lines))]

        cut_path = os.path.join(self.dir, "cut.siql")
        for cut in range(len(data) + 1):
            with self.subTest(cut=cut):
                with open(cut_path, "wb") as f:
                    f.write(data[:cut])
                complete = max([e for e in ends if e <= cut], default=0)

                t = Table(file=cut_path)
                self.assertConsistent(t)
                self.assertEqual(os.path.getsize(cut_path), complete)

                expected_path = os.path.join(self.dir, "expected.siql")
                with open(expected_path, "wb") as f:
                    f.write(data[:complete])
                self.assertEqual(snapshot(t), snapshot(Table(file=expected_path)))
                os.remove(expected_path)

                # The recovered file must accept new writes and reload cleanly
                if "name" in t.cols:
                    t.add(name="after crash")
                    self.assertReloadsTo(t, cut_path)


if __name__ == "__main__":
    unittest.main()
