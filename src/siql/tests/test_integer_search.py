"""Integer seeks must cover earlier matches, holes, custom equality, and live mutations."""
import random
import tempfile
import unittest

from .. import Table, TableDiff
from ..models import AddRow, DropRow, RenameCol, DropCol
from ..models.prefix import integer_field
from ..controllers import Interpreter


class MatchesEverything:
    def __eq__(self, other):
        return True


class IntegerSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/rows.siql'
        self.table = Table(file=self.path, integer_search=True, lazy_add=True, lazy_delete=True)
        self.table.addCols(name=str, age=int, blob=bytes)
        TableDiff([AddRow(i, {'name': 'Ann' + str(i % 5), 'age': i % 19,
                             'blob': bytes([i % 7, i % 11])}) for i in range(100)]).apply(self.table)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, criteria):
        expected = [i for i, row in enumerate(self.table.rows)
                    if all(value == row.col(column).value for column, value in criteria.items())]
        self.assertEqual(self.table.find_where(**criteria), expected)
        index = self.table._compressed_index
        positions = {id(row): i for i, row in enumerate(self.table.rows)}
        self.assertEqual(sorted(positions[id(row)] for row in index.find(criteria, seek='lower')), expected)

    def test_full_values_holes_and_absent_values(self):
        for row in self.table.rows:
            values = {column: row.col(column).value for column in self.table.cols}
            for criteria in [values, {'name': values['name'], 'blob': values['blob']},
                             {'age': values['age'], 'blob': values['blob']}]:
                self.check(criteria)
        self.check({'name': 'a very long unknown name', 'age': 1})
        self.check({'name': 'Ann1', 'age': -999})
        self.assertEqual(self.table.find('name', 'Ann1'), [i for i in range(100) if i % 5 == 1])

    def test_normalization_and_distinct_trailing_zero_bits(self):
        self.assertEqual(integer_field(1), integer_field(1.0))
        self.assertEqual(integer_field(True), integer_field(1))
        self.assertEqual(integer_field(-0.0), integer_field(0))
        self.assertNotEqual(integer_field('a'), integer_field('a\0'))
        self.assertNotEqual(integer_field(b'a'), integer_field(b'a\0'))
        self.table.rows[0].col('name').value = 'a'
        self.table.rows[1].col('name').value = 'a\0'
        self.check({'name': 'a', 'age': 0})
        self.check({'name': 'a\0', 'age': 1.0})
        self.assertNotEqual(self.table.rows[0]._integer_signature, self.table.rows[1]._integer_signature)
        self.assertEqual(self.table._compressed_index.keys, sorted(self.table._compressed_index.keys))

    def test_estimate_backtracks_for_matches_before_jump(self):
        table = Table(file=self.tmp.name + '/backtrack.siql', integer_search=True)
        table.addCols(name=str, age=int, blob=bytes)
        for age in range(1, 10):
            table.add(name='A', age=age, blob=b'x')
        self.assertEqual(table.find_where(name='A', blob=b'x'), list(range(9)))
        self.assertGreater(table.integer_info['last_backtracked_groups'], 0)

    def test_mutation_schema_rollback_and_reload(self):
        rng = random.Random(97)
        for step in range(80):
            if step % 3 == 0:
                self.table.add(name='Zed', age=step, blob=b'z')
            elif step % 3 == 1:
                TableDiff([DropRow(rng.randrange(len(self.table.rows)))]).apply(self.table)
            else:
                self.table.rows[rng.randrange(len(self.table.rows))].col('name').value = 'Zed'
            self.check({'name': 'Zed', 'age': step})
            self.check({'name': 'Zed', 'blob': b'z'})
        with self.assertRaises(IndexError):
            TableDiff([DropRow(0), AddRow(0, {'unknown': 1})]).apply(self.table)
        TableDiff([RenameCol('name', 'label'), DropCol('blob')]).apply(self.table)
        self.check({'label': 'Zed', 'age': 0})
        self.table.compact()
        loaded = Table(file=self.path, integer_search=True)
        self.assertEqual(loaded.find_where(label='Zed', age=0), self.table.find_where(label='Zed', age=0))
        self.table.tree = False
        self.assertFalse(self.table.integer_info['active'])
        self.table.tree = True
        self.check({'label': 'Zed', 'age': 0})
        self.table.compressed_search = True
        self.assertTrue(self.table.integer_info['active'])
        self.table.integer_search = False
        self.assertTrue(self.table.compressed_info['active'])
        self.assertFalse(hasattr(self.table.rows[0], '_integer_signature'))

    def test_custom_mutable_values_and_sql(self):
        self.table.rows[0].col('age').value = MatchesEverything()
        self.check({'name': 'Ann0', 'age': 999})
        self.check({'age': MatchesEverything(), 'blob': b'\0\0'})
        buffer = bytearray(b'ab')
        self.table.rows[0].col('blob').value = memoryview(buffer)
        self.check({'name': 'Ann0', 'blob': b'ab'})
        buffer[1] = ord('z')
        self.check({'name': 'Ann0', 'blob': b'az'})
        interpreter = Interpreter(self.tmp.name)
        interpreter.execute('CREATE TABLE p (name str, age int)')
        interpreter.execute("INSERT INTO p VALUES ('Ann', 1), ('Ann', 2), ('Bob', 1)")
        interpreter.database.table('p').integer_search = True
        self.assertEqual(interpreter.execute("SELECT age FROM p WHERE name = 'Ann' AND age = 1").rows, [[1]])


if __name__ == '__main__':
    unittest.main()
