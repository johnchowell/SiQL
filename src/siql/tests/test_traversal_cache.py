"""Inspected nodes become budgeted, invalidation-safe exact lookup entries."""
import random
import tempfile
import unittest
from unittest import mock

from .. import Table, TableDiff, _speedups
from ..helpers.search import PySkipList
from ..models import AddRow, DropRow


class TraversalCacheTests(unittest.TestCase):
    def test_trace_contains_every_compared_node_including_stopping_nodes(self):
        # Force a one-lane reference list so the complete comparison trace is known.
        lanes = PySkipList()
        with mock.patch('siql.helpers.search.random.random', return_value=1.0):
            for i in range(10):
                lanes.insert(i, i)
        ids, visited = lanes.get_with_trace(5)
        self.assertEqual(ids, {5})
        self.assertEqual(list(visited), list(range(6)))
        self.assertEqual(lanes.get_with_trace(5.5)[1], {i: {i} for i in range(7)})

    def test_native_and_reference_traces_preserve_exact_results(self):
        for implementation in [PySkipList, _speedups.SkipList]:
            lanes = implementation()
            for i in range(100):
                lanes.insert(i, i)
            for key in [-1, 3, 49.5, 1000]:
                ids, visited = lanes.get_with_trace(key)
                self.assertEqual(ids, lanes.get(key))
                self.assertTrue(visited)
                self.assertTrue(all(lanes.get(v) == ids for v, ids in visited.items()))
            targets = [99, 0, 50, 49, 100, 50]
            for chained in [True, False]:
                found, visited = lanes.get_many(targets, chained=chained, trace=True)
                self.assertEqual(found, [lanes.get(v) for v in targets])
                self.assertTrue({0, 50, 99}.issubset(visited))
            lanes.remove(50, 50)
            self.assertEqual(lanes.get_with_trace(50)[0], set())
            self.assertNotIn(50, lanes.get_with_trace(50)[1])

    def make(self, directory, budget=100_000_000):
        table = Table(file=directory + '/rows.siql', lazy_delete=True,
                      cache_max_bytes=budget, cache_traversed_nodes=True)
        table.addCols(name=str, n=int)
        TableDiff([AddRow(i, {'name': f'item {i:04}', 'n': i}) for i in range(200)]).apply(table)
        return table

    def test_passed_value_hits_cache_without_arriving_at_it_first(self):
        with tempfile.TemporaryDirectory() as directory:
            table = self.make(directory)
            self.assertEqual(table.find('name', 'item 0199'), [199])
            cache = table._query_cache
            passed = [key[1] for key in cache._entries if key[1] != 'item 0199']
            self.assertTrue(passed)
            target = passed[0]
            with mock.patch.object(type(table.index['name']), '_find', side_effect=AssertionError('tree miss')):
                self.assertEqual(table.find('name', target),
                                 [i for i, row in enumerate(table.rows) if row.col('name').value == target])
            self.assertGreater(table.cache_info['traversal_admissions'], 1)
            self.assertLessEqual(table.cache_info['bytes'], table.cache_max_bytes)

    def test_batch_eviction_resize_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            table = self.make(directory, 3000)
            rng = random.Random(86)
            for step in range(60):
                if step % 3 == 0:
                    table.rows[rng.randrange(len(table.rows))].col('name').value = 'item changed'
                elif step % 3 == 1:
                    TableDiff([DropRow(rng.randrange(len(table.rows)))]).apply(table)
                else:
                    table.add(name='item changed', n=1000 + step)
                queries = ['item changed', f'item {rng.randrange(200):04}', 'item missing']
                expected = [[i for i, r in enumerate(table.rows) if r.col('name').value == value]
                            for value in queries]
                self.assertEqual(table.find_batch('name', queries), expected)
                self.assertLessEqual(table.cache_info['bytes'], table.cache_max_bytes)
            table.cache_max_bytes = 1
            self.assertEqual(table.cache_info['entries'], 0)
            self.assertEqual(table.find('name', 'item changed'),
                             [i for i, r in enumerate(table.rows) if r.col('name').value == 'item changed'])
            table.cache_traversed_nodes = False
            self.assertFalse(table.cache_info['cache_traversed_nodes'])
            with self.assertRaises(ValueError):
                table.cache_traversed_nodes = 1


if __name__ == '__main__':
    unittest.main()
