"""Deferred tree insertion must retain immediate visibility and mutation correctness."""
import random
import tempfile
import unittest

from .. import Table, TableDiff
from ..models import AddRow, DropRow, DropCol, RenameCol
from ..controllers import Interpreter


class MatchesEverything:
    def __eq__(self, other):
        return True


class LazyAddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/rows.siql'
        self.table = Table(file=self.path, lazy_add=True, lazy_delete=True)
        self.table.addCols(name=str, age=int)

    def tearDown(self):
        self.tmp.cleanup()

    def expected(self, column, value):
        return [i for i, row in enumerate(self.table.rows) if value == row.col(column).value]

    def test_immediate_visibility_and_branch_local_drain(self):
        self.table.add(name='Ann', age=31)
        self.table.add(name='Bob', age=42)
        node = self.table.index['name']
        self.assertEqual(len(node), 2)
        self.assertEqual(len(node.pending), 2)
        self.assertEqual(node.branches, {})
        self.assertEqual(self.table.find('name', 'Ann'), [0])
        self.assertEqual(len(node.pending), 1)
        self.assertEqual(len(self.table.index['age'].pending), 2)
        self.assertEqual(node['B'], [self.table.rows[1]])
        self.assertEqual(node.pending, set())
        loaded = Table(file=self.path, lazy_add=True)
        self.assertEqual(loaded.find('age', 42), [1])

    def test_pending_edits_and_cached_misses(self):
        self.assertEqual(self.table.find('name', 'Ann'), [])
        self.table.add(name='Ann', age=1)
        self.table.rows[0].col('name').value = 'Zed'
        self.table.rows[0].col('age').value = 19
        self.assertEqual(self.table.find('name', 'Ann'), [])
        self.assertEqual(self.table.find('name', 'Zed'), [0])
        self.assertEqual(self.table.find('name', 'Zed'), [0])
        self.assertGreater(self.table.cache_info['hits'], 0)
        self.assertEqual(self.table.find('age', 1), [])
        self.assertEqual(self.table.find('age', 19), [0])
        self.table.add(name='Zed', age=19)
        self.assertEqual(self.table.find('name', 'Zed'), [0, 1])

    def test_delete_before_indexing_and_delete_undo(self):
        self.table.add(name='Ann', age=1)
        removed = self.table.rows[0]
        TableDiff([DropRow(0)]).apply(self.table)
        self.assertEqual(self.table.index['name'].pending, set())
        self.assertEqual(self.table.index['name'].dead, set())
        self.assertIsNone(removed.col('name')._tree)
        self.table.add(name='Ann', age=1)
        self.table.find('name', 'Ann')
        with self.assertRaises(IndexError):
            TableDiff([DropRow(0), AddRow(0, {'unknown': 1})]).apply(self.table)
        self.assertEqual(self.table.find('name', 'Ann'), [0])
        with self.assertRaises(IndexError):
            TableDiff([AddRow(0, {'name': 'Bob'}), AddRow(0, {'unknown': 1})]).apply(self.table)
        self.assertEqual(self.table.find('name', 'Bob'), [])
        self.assertEqual(len(self.table.index['name']), 1)

    def test_batch_custom_values_and_traversal_cache(self):
        self.table.cache_traversed_nodes = True
        for name, age in [('Ann', 1), ('Bob', 2), ('Ann', 3), ('Cy', 4)]:
            self.table.add(name=name, age=age)
        values = ['Cy', 'Ann', 'missing', 'Bob', 'Ann']
        self.assertEqual(self.table.find_batch('name', values), [self.expected('name', v) for v in values])
        self.assertEqual(self.table.index['name'].pending, set())
        self.table.rows[0].col('age').value = MatchesEverything()
        self.assertEqual(self.table.find('age', 99), [0])
        self.assertEqual(self.table.find('age', MatchesEverything()), list(range(4)))
        self.table.add(name='Null')
        self.assertEqual(self.table.find('age', None), self.expected('age', None))

    def test_schema_switches_and_compressed_search(self):
        self.table.compressed_search = True
        self.table.add(name='Ann', age=1)
        self.table.rows[0].col('name').value = 'Zed'
        self.assertEqual(self.table.find_where(name='Zed', age=1), [0])
        self.assertEqual(len(self.table.index['name'].pending), 1)
        TableDiff([RenameCol('name', 'label')]).apply(self.table)
        self.assertEqual(self.table.find('label', 'Zed'), [0])
        self.table.add(label='Bob', age=2)
        TableDiff([DropCol('age')]).apply(self.table)
        self.table.addCols(age=int)
        self.assertEqual(self.table.find_where(label='Bob', age=None), [1])
        self.table.tree = False
        self.table.add(label='Cy', age=3)
        self.assertEqual(self.table.find('label', 'Cy'), [2])
        self.table.tree = True
        self.assertEqual(self.table.find('label', 'Cy'), [2])
        self.table.add(label='Dee', age=4)
        self.table.lazy_add = False
        self.assertTrue(all(not node.pending for node in self.table.index.values()))
        self.table.add(label='Eve', age=5)
        self.assertTrue(all(not node.pending for node in self.table.index.values()))
        with self.assertRaises(ValueError):
            self.table.lazy_add = 'yes'

    def test_queue_bound_purge_and_random_mutations(self):
        rng = random.Random(55)
        for i in range(250):
            self.table.add(name='Ann' + str(i % 9), age=i % 17)
        self.assertLessEqual(len(self.table.index['name'].pending), len(self.table.rows) // 2)
        for step in range(200):
            if step % 3 == 0:
                self.table.add(name='Zed' + str(step % 5), age=step % 17)
            elif step % 3 == 1:
                TableDiff([DropRow(rng.randrange(len(self.table.rows)))]).apply(self.table)
            else:
                self.table.rows[rng.randrange(len(self.table.rows))].col('age').value = step % 17
            for column, value in [('name', 'Zed' + str(step % 5)), ('age', step % 17)]:
                self.assertEqual(self.table.find(column, value), self.expected(column, value))
        self.table.purge()
        for column, node in self.table.index.items():
            self.assertEqual(node.pending, set())
            self.assertEqual(node.dead, set())
            self.assertEqual(len(node), len(self.table.rows))
            for row in self.table.rows:
                self.assertIs(row.col(column)._tree, node)

    def test_sql_sees_pending_rows(self):
        interpreter = Interpreter(self.tmp.name)
        interpreter.execute('CREATE TABLE p (name str, age int)')
        table = interpreter.database.table('p')
        table.lazy_add = True
        table.lazy_delete = True
        interpreter.execute("INSERT INTO p VALUES ('Ann', 1), ('Bob', 2)")
        self.assertEqual(interpreter.execute("SELECT age FROM p WHERE name = 'Ann'").rows, [[1]])
        self.assertEqual(interpreter.execute('SELECT age FROM p WHERE age > 0 ORDER BY age').rows, [[1], [2]])
        interpreter.execute("UPDATE p SET age = 9 WHERE name = 'Bob'")
        self.assertEqual(interpreter.execute('SELECT name FROM p WHERE age = 9').rows, [['Bob']])
        interpreter.execute('DELETE FROM p WHERE age = 1')
        self.assertEqual(interpreter.execute('SELECT name FROM p WHERE age = 1').rows, [])


if __name__ == '__main__':
    unittest.main()
