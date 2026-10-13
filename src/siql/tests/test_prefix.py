"""Hidden all-column signatures must narrow candidates without losing exact matches."""
import random
from pathlib import Path
import tempfile
import unittest

from .. import Table, TableDiff
from ..models import AddRow, DropRow, DropCol, RenameCol
from ..models.prefix import prefix
from ..controllers import Interpreter


class EqualToEverything:
    def __eq__(self, other):
        return True


class PrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/rows.siql'
        self.table = Table(file=self.path, compressed_search=True, lazy_delete=True, cache_max_bytes=0)
        self.table.addCols(name=str, age=object, blob=bytes)
        TableDiff([AddRow(i, {'name': ('Ann' if i % 2 else 'Bob') + str(i % 5),
                             'age': i % 40, 'blob': bytes([i % 8, i % 13])}) for i in range(200)]).apply(self.table)

    def tearDown(self):
        self.tmp.cleanup()

    def exact(self, criteria):
        return [i for i, row in enumerate(self.table.rows)
                if all(value == row.col(column).value for column, value in criteria.items())]

    def assert_matches(self, criteria):
        expected = self.exact(criteria)
        self.assertEqual(self.table.find_where(**criteria), expected)
        if len(criteria) == 1:
            column, value = next(iter(criteria.items()))
            self.assertEqual(self.table.find(column, value), expected)

    def test_prefix_normalization_and_hidden_field(self):
        self.assertEqual(prefix(-12), prefix(12.0))
        self.assertEqual(prefix(True), prefix(1))
        self.assertEqual(prefix('😀hello'), ('text', '😀'))
        self.assertEqual(prefix(b'\xffabc'), ('byte', 255))
        self.assertEqual(prefix(bytearray(b'\xff')), prefix(b'\xff'))
        self.assertNotEqual(prefix(''), prefix(None))
        self.assertNotEqual(prefix('1'), prefix(1))
        self.assertIsNone(prefix(float('nan')))
        self.assert_matches({'name': 'Ann1', 'age': 1})
        self.assertEqual(len(self.table.rows[0]._compressed_signature), 12)
        self.assertNotIn('_compressed_signature', self.table.struct.column_names)
        self.assertNotIn('_compressed_signature', Path(self.path).read_text(encoding='utf-8'))

    def test_single_partial_and_full_queries(self):
        for i in range(200):
            row = self.table.rows[i]
            full = {name: row.col(name).value for name in self.table.struct.column_names}
            for criteria in [full, {'name': full['name']}, {'age': full['age']},
                             {'name': full['name'], 'blob': full['blob']}]:
                self.assert_matches(criteria)
        self.assert_matches({'name': 'missing', 'age': 9})
        self.assertEqual(self.table.find_where(), list(range(200)))
        self.assertEqual(self.table.find_batch('name', ['Ann1', 'Bob0', 'missing']),
                         [self.exact({'name': v}) for v in ['Ann1', 'Bob0', 'missing']])

    def test_group_trees_preserve_primary_cell_observers(self):
        self.table.find_where(name='Ann1', age=1)
        for column in self.table.struct.column_names:
            self.assertTrue(all(cell._tree is self.table.index[column] for cell in self.table.cols[column]))
        self.table.rows[1].col('name').value = 'Zed'
        self.assert_matches({'name': 'Zed'})
        self.assertEqual(self.table.index['name'].find('Zed'), [self.table.rows[1]])

    def test_single_column_search_keeps_primary_tree_and_cache(self):
        self.table.cache_max_bytes = 100000
        self.assert_matches({'name': 'Ann1'})
        self.table.find('name', 'Ann1')
        self.assertGreater(self.table.cache_info['hits'], 0)
        values = ['Ann1', 'Bob0', 'missing']
        for order in ['tree', 'input']:
            self.assertEqual(self.table.find_batch('name', values, order=order),
                             [self.exact({'name': value}) for value in values])
        self.assertEqual(self.table.compressed_info['group_column_trees'], 0)
        # A single-column edit/search can leave grouping deferred until a composite lookup.
        self.table.rows[1].col('name').value = 'Zed'
        self.assert_matches({'name': 'Zed'})
        self.assert_matches({'name': 'Zed', 'age': 1})
        self.assertGreater(self.table.compressed_info['group_column_trees'], 0)

    def test_edits_within_prefix_and_across_prefixes(self):
        self.table.cache_max_bytes = 100000
        self.table.find_where(name='Ann1', age=1)
        self.table.find('name', 'Ann1')
        self.table.rows[1].col('name').value = 'Anna changed'
        self.assert_matches({'name': 'Anna changed', 'age': 1})
        self.assert_matches({'name': 'Ann1'})
        self.table.rows[1].col('name').value = 'Zed'
        self.assert_matches({'name': 'Zed'})
        self.table.rows[1].col('age').value = 99
        self.assert_matches({'name': 'Zed', 'age': 99})
        self.table.rows[1].col('blob').value = b'\xff!'
        self.assert_matches({'name': 'Zed', 'blob': b'\xff!'})

    def test_custom_and_mutable_values_use_conservative_routes(self):
        self.table.rows[1].col('age').value = EqualToEverything()
        for value in [3, None, 'anything']:
            self.assert_matches({'name': 'Ann1', 'age': value})
        self.assert_matches({'age': EqualToEverything()})
        self.table.rows[1].col('blob').value = bytearray(b'abc')
        self.assert_matches({'name': 'Ann1', 'blob': b'abc'})
        self.table.rows[1].col('blob').value[0] = ord('z')
        self.assert_matches({'blob': b'zbc'})
        buffer = bytearray(b'mno')
        self.table.rows[1].col('blob').value = memoryview(buffer)
        self.assert_matches({'blob': b'mno'})
        buffer[0] = ord('q')
        self.assert_matches({'blob': b'qno'})

    def test_schema_changes_rollback_and_reload(self):
        self.table.find_where(name='Ann1', age=1)
        TableDiff([RenameCol('name', 'label')]).apply(self.table)
        self.assert_matches({'label': 'Ann1', 'age': 1})
        with self.assertRaises(IndexError):
            TableDiff([DropCol('label'), AddRow(0, {'unknown': 'bad'})]).apply(self.table)
        self.assert_matches({'label': 'Ann1', 'age': 1})
        TableDiff([DropCol('blob')]).apply(self.table)
        self.table.addCols(blob=bytes)
        self.table.rows[0].col('blob').value = b'new'
        self.assert_matches({'blob': b'new'})
        self.table.compact()
        loaded = Table(file=self.path, compressed_search=True)
        self.assertEqual(loaded.find_where(label='Ann1', age=1), self.exact({'label': 'Ann1', 'age': 1}))
        self.assertEqual(loaded.compressed_info['row_signature_width'], 12)

    def test_random_mutations_and_runtime_switches(self):
        rng = random.Random(87)
        for step in range(80):
            if step % 3 == 0:
                self.table.add(name='Ann added', age=step, blob=b'\x00x')
            elif step % 3 == 1:
                TableDiff([DropRow(rng.randrange(len(self.table.rows)))]).apply(self.table)
            else:
                self.table.rows[rng.randrange(len(self.table.rows))].col('name').value = 'Ann added'
            self.assert_matches({'name': 'Ann added', 'age': step})
            self.assert_matches({'age': step})
        self.table.tree = False
        self.assertFalse(self.table.compressed_info['active'])
        self.table.rows[0].col('name').value = 'off edit'
        self.assert_matches({'name': 'off edit'})
        self.table.tree = True
        self.assert_matches({'name': 'off edit'})
        self.table.compressed_search = False
        self.assertFalse(hasattr(self.table.rows[0], '_compressed_signature'))
        self.assert_matches({'name': 'Ann added', 'age': 0})
        self.table.compressed_search = True
        self.assert_matches({'name': 'off edit'})
        with self.assertRaises(KeyError):
            self.table.find_where(unknown=1)

    def test_sql_conjunction_and_repeated_column_fallback(self):
        interpreter = Interpreter(self.tmp.name)
        interpreter.execute('CREATE TABLE p (name str, age int)')
        interpreter.execute("INSERT INTO p VALUES ('Ann', 1), ('Ann', 2), ('Bob', 1)")
        table = interpreter.database.table('p')
        queries = ["SELECT age FROM p WHERE name = 'Ann' AND age = 1",
                   "SELECT age FROM p WHERE 'Ann' = name AND 2 = age",
                   "SELECT age FROM p WHERE name = 'Ann' AND age > 1",
                   "SELECT age FROM p WHERE name = 'Ann' AND name = 'Bob'",
                   "SELECT age FROM p WHERE name = 'Ann' OR age = 1"]
        expected = [interpreter.execute(query).rows for query in queries]
        table.compressed_search = True
        self.assertEqual([interpreter.execute(query).rows for query in queries], expected)


if __name__ == '__main__':
    unittest.main()
