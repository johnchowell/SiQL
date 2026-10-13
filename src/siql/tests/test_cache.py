"""LRU bounds and transparent live-row exact-result caching."""
import random
import sys
import tempfile
import unittest
from unittest import mock

from .. import Table, TableDiff
from ..models import AddRow, DropRow, RenameCol, DropCol, SetCell
from ..models.cache import QueryCache, DEFAULT_CACHE_BYTES
from ..controllers import Interpreter


class EqualToEverything:
    def __eq__(self, other):
        return True


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.table = Table(file=self.tmp.name + '/rows.siql', lazy_delete=True)
        self.table.addCols(name=str, age=object)
        TableDiff([AddRow(i, {'name': name, 'age': i + 1})
                   for i, name in enumerate(['Ann', 'Bob', 'Ann', 'Cat'])]).apply(self.table)

    def tearDown(self):
        self.tmp.cleanup()

    def exact(self, column, value):
        return [i for i, row in enumerate(self.table.rows) if value == row.col(column).value]

    def test_hits_share_live_rows_and_protect_returned_lists(self):
        self.assertEqual(self.table.cache_max_bytes, DEFAULT_CACHE_BYTES)
        self.assertEqual(self.table.find('name', 'Ann'), [0, 2])
        node = self.table.index['name']
        self.table.rows[0].col('age').value = 99
        with mock.patch.object(type(node), '_find', side_effect=AssertionError('unexpected tree traversal')):
            result = self.table.find('name', 'Ann')
            result.clear()
            self.assertEqual(self.table.find('name', 'Ann'), [0, 2])
            self.assertIs(list(self.table.select('Ann', 'name'))[0], self.table.rows[0])
            self.assertEqual(list(self.table.select('Ann', 'name'))[0].col('age').value, 99)
        self.assertGreater(self.table.cache_info['hits'], 0)

    def test_edits_adds_deletes_and_column_changes(self):
        self.table.find('name', 'Ann')
        self.table.find('name', 'missing')
        self.table.rows[1].col('name').value = 'Ann'
        self.assertEqual(self.table.find('name', 'Ann'), [0, 1, 2])
        self.table.add(name='missing', age=10)
        self.assertEqual(self.table.find('name', 'missing'), [4])
        TableDiff([DropRow(0)]).apply(self.table)
        self.assertEqual(self.table.find('name', 'Ann'), [0, 1])
        TableDiff([RenameCol('name', 'label')]).apply(self.table)
        self.assertEqual(self.table.find('label', 'Ann'), [0, 1])
        TableDiff([DropCol('label')]).apply(self.table)
        self.assertEqual(self.table.cache_info['entries'], 0)
        self.table.addCols(label=str)
        self.assertEqual(self.table.find('label', None), list(range(4)))

    def test_cache_is_byte_bounded_lru_and_runtime_resize_is_immediate(self):
        cache, column = QueryCache(), object()
        row = object()
        cache.put(column, 'a', [row])
        entry_bytes = cache.info['bytes']
        cache.max_bytes = entry_bytes * 2
        cache.put(column, 'b', [row])
        self.assertEqual(cache.get(column, 'a'), (row,))
        cache.put(column, 'c', [row])
        self.assertIsNone(cache.get(column, 'b'))
        self.assertEqual(cache.info['entries'], 2)
        self.assertLessEqual(cache.info['bytes'], cache.max_bytes)
        cache.max_bytes = entry_bytes
        self.assertEqual(cache.info['entries'], 1)
        self.assertIsNone(cache.get(column, 'a'))
        self.assertEqual(cache.get(column, 'c'), (row,))
        cache.put(column, 'huge' * 10000, [row])
        self.assertEqual(cache.get(column, 'c'), (row,))
        cache.max_bytes = 0
        self.assertEqual(cache.info['bytes'], 0)
        self.assertEqual(cache.info['entries'], 0)
        for limit in [-1, True, 1.5, '100MB']:
            with self.assertRaises(ValueError):
                cache.max_bytes = limit

    def test_batch_hits_and_recency_follow_input_order(self):
        self.assertEqual(self.table.find_batch('name', ['Cat', 'Ann', 'Bob', 'Ann']),
                         [[3], [0, 2], [1], [0, 2]])
        node = self.table.index['name']
        with mock.patch.object(type(node), '_find', side_effect=AssertionError('unexpected scan')):
            self.assertEqual(self.table.find_batch('name', ['Bob', 'Cat', 'Ann']), [[1], [3], [0, 2]])
        cached = self.table._query_cache
        self.assertEqual([key[1] for key in cached._entries], ['Bob', 'Cat', 'Ann'])
        self.table.cache_max_bytes = 0
        self.assertEqual(self.table.find_batch('name', ['Ann', 'new']), [[0, 2], []])

    def test_shrinking_releases_backing_mapping_capacity(self):
        cache, column = QueryCache(), object()
        for i in range(1000):
            cache.put(column, i, [])
        old_map_bytes = sys.getsizeof(cache._entries)
        cache.max_bytes = 1000
        self.assertLess(sys.getsizeof(cache._entries), old_map_bytes / 10)
        self.assertLessEqual(cache.info['bytes'], 1000)
        self.assertEqual(cache.info['entries'], 1)

    def test_mutable_custom_and_nan_values_never_yield_stale_hits(self):
        self.table.rows[0].col('age').value = [1]
        self.assertEqual(self.table.find('age', [1]), [0])
        self.table.rows[0].col('age').value.append(2)
        self.assertEqual(self.table.find('age', [1]), [])
        self.assertEqual(self.table.find('age', [1, 2]), [0])
        self.table.find('age', 2)
        self.assertEqual(self.table.cache_info['entries'], 0)
        self.table.rows[0].col('age').value = EqualToEverything()
        self.assertEqual(self.table.find('age', 2), [0, 1])
        self.table.rows[0].col('age').value = float('nan')
        self.assertEqual(self.table.find('age', float('nan')), [])
        self.assertEqual(self.table.cache_info['entries'], 0)
        self.table.rows[0].col('age').value = 1
        self.table.find('age', True)
        hits = self.table.cache_info['hits']
        self.assertEqual(self.table.find('age', 1.0), [0])
        self.assertEqual(self.table.cache_info['hits'], hits + 1)

    def test_disable_clear_and_tree_toggle(self):
        self.table.find('name', 'Ann')
        self.table.clear_cache()
        self.assertEqual(self.table.cache_info['bytes'], 0)

        self.table.find('name', 'Ann')
        self.table.tree = False
        self.assertEqual(self.table.cache_info['entries'], 0)
        self.table.rows[1].col('name').value = 'Ann'
        self.assertEqual(self.table.find('name', 'Ann'), [0, 1, 2])
        self.table.tree = True
        self.assertEqual(self.table.find('name', 'Ann'), [0, 1, 2])
        self.table.cache_max_bytes = 1
        self.assertEqual(self.table.cache_info['entries'], 0)
        self.table.find('name', 'Ann')
        self.assertEqual(self.table.cache_info['bytes'], 0)

    def test_sql_equality_and_in_share_the_cache_and_see_updates(self):
        interpreter = Interpreter(self.tmp.name)
        interpreter.execute('CREATE TABLE people (name str, age int)')
        interpreter.execute("INSERT INTO people VALUES ('Ann', 1), ('Bob', 2)")
        table = interpreter.database.table('people')
        query = "SELECT age FROM people WHERE name = 'Ann'"
        self.assertEqual(interpreter.execute(query).rows, [[1]])
        hits = table.cache_info['hits']
        self.assertEqual(interpreter.execute(query).rows, [[1]])
        self.assertGreater(table.cache_info['hits'], hits)
        self.assertEqual(interpreter.execute("SELECT name FROM people WHERE name IN ('Ann', 'Bob')").rows,
                         [['Ann'], ['Bob']])
        interpreter.execute("UPDATE people SET age = 3 WHERE name = 'Ann'")
        self.assertEqual(interpreter.execute(query).rows, [[3]])
        interpreter.execute("UPDATE people SET name = 'Anne' WHERE name = 'Ann'")
        self.assertEqual(interpreter.execute(query).rows, [])

    def test_rollback_and_random_mutations_match_uncached_scan(self):
        self.table.find('name', 'Ann')
        with self.assertRaises(IndexError):
            TableDiff([SetCell(0, 'name', 'other'), SetCell(999, 'name', 'bad')]).apply(self.table)
        self.assertEqual(self.table.find('name', 'Ann'), [0, 2])
        rng = random.Random(84)
        self.table.cache_max_bytes = 3000
        for i in range(60):
            if i % 3 == 0:
                self.table.add(name=rng.choice(['Ann', 'Bob', 'Cat']), age=i)
            elif i % 3 == 1:
                TableDiff([DropRow(rng.randrange(len(self.table.rows)))]).apply(self.table)
            else:
                self.table.rows[rng.randrange(len(self.table.rows))].col('name').value = 'Cat'
            queries = ['Ann', 'Bob', 'Cat', 'missing']
            self.assertEqual(self.table.find_batch('name', queries), [self.exact('name', v) for v in queries])
            self.assertLessEqual(self.table.cache_info['bytes'], self.table.cache_max_bytes)


if __name__ == '__main__':
    unittest.main()
