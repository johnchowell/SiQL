"""Chained exact lookups: arbitrary traversal, mutation safety, and input-order restoration."""
import random
import tempfile
import unittest

from .. import Table, TableDiff, _speedups
from ..helpers.search import PySkipList
from ..models import AddRow, DropRow


class EqualToEverything:
    def __eq__(self, other):
        return True


class CountedKey:
    comparisons = 0

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        type(self).comparisons += 1
        return self.value < other.value

    def __eq__(self, other):
        type(self).comparisons += 1
        return self.value == other.value


class BatchTests(unittest.TestCase):
    def test_native_and_reference_fingers(self):
        rng = random.Random(84)
        for implementation in [PySkipList, _speedups.SkipList]:
            with self.subTest(implementation=implementation):
                lanes = implementation()
                inserted = rng.sample(range(4000), 1300)
                for value in inserted:
                    lanes.insert(value, value)
                    lanes.insert(value, value + 5000)
                queries = rng.sample(range(4500), 800)
                for targets in [queries, sorted(queries), sorted(queries, reverse=True),
                                [inserted[0]] * 30, [0, 5000, 1, 4999, -1], []]:
                    expected = [lanes.get(value) for value in targets]
                    for chained in [True, False]:
                        self.assertEqual(lanes.get_many(iter(targets), chained=chained), expected)
                for value in inserted[:100]:
                    lanes.remove(value, value)
                    lanes.remove(value, value + 5000)
                lanes.insert(5001, 8)
                self.assertEqual(lanes.get_many(sorted(queries + [5001])),
                                 [lanes.get(v) for v in sorted(queries + [5001])])

    def test_chaining_reduces_comparisons_for_adjacent_targets(self):
        for implementation in [PySkipList, _speedups.SkipList]:
            lanes = implementation()
            for i in range(1000):
                lanes.insert(CountedKey(i), i)
            targets = [CountedKey(i) for i in range(200, 800)]
            CountedKey.comparisons = 0
            independent = lanes.get_many(targets, chained=False)
            baseline = CountedKey.comparisons
            CountedKey.comparisons = 0
            self.assertEqual(lanes.get_many(targets), independent)
            self.assertLess(CountedKey.comparisons, baseline)

    def test_table_batch_matches_exact_search_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as directory:
            table = Table(file=directory + '/rows.siql', lazy_delete=True)
            table.addCols(value=object)
            values = ['alpha', 'alpine', 'alpha', 'beta', '', 1, 1.0, True, -1, 12,
                      '1', None, [1], float('inf'), float('nan'), EqualToEverything()]
            TableDiff([AddRow(i, {'value': None}) for i in range(len(values))]).apply(table)
            for row, value in zip(table.rows, values):
                row.col('value').value = value
            queries = ['alpine', 'absent', 'alpha', None, 1.0, True, -1, 12, '1', '',
                       [1], EqualToEverything(), float('nan'), 'alpha']
            for edit in range(3):
                for tree in [True, False]:
                    table.tree = tree
                    expected = [table.find('value', value) for value in queries]
                    for order in ['tree', 'input']:
                        for chained in [True, False]:
                            with self.subTest(edit=edit, tree=tree, order=order, chained=chained):
                                self.assertEqual(table.find_batch(0, iter(queries), order=order,
                                                                  chained=chained), expected)
                    self.assertEqual(table.find_batch('value', []), [])
                table.tree = True
                table.rows[0].col('value').value = f'altered {edit}'
                TableDiff([DropRow(1)]).apply(table)
                table.add(value='alpha')
            with self.assertRaises(ValueError):
                table.find_batch('value', ['alpha'], order='unknown')
            with self.assertRaises(KeyError):
                table.find_batch('unknown', ['alpha'])

    def test_failed_batch_does_not_retain_a_finger(self):
        for implementation in [PySkipList, _speedups.SkipList]:
            lanes = implementation()
            lanes.insert(1, 1)
            with self.assertRaises(TypeError):
                lanes.get_many([1, 'incomparable'])
            lanes.remove(1, 1)
            lanes.insert(2, 2)
            self.assertEqual(lanes.get_many([1, 2]), [set(), {2}])

    def test_dense_table_batches_after_lazy_deletes_and_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            table = Table(file=directory + '/rows.siql', lazy_delete=True)
            table.addCols(name=str)
            TableDiff([AddRow(i, {'name': f'item {i:04}'}) for i in range(500)]).apply(table)
            queries = [f'item {i:04}' for i in range(150, 230)] + ['missing', 'item 0200'] * 4
            random.Random(84).shuffle(queries)
            for edit in range(3):
                if edit:
                    TableDiff([DropRow(170)] * 10).apply(table)
                    table.rows[180].col('name').value = 'item 0200'
                    table.add(name='item 0200')
                expected = [[i for i, row in enumerate(table.rows) if value == row.col('name').value]
                            for value in queries]
                for order in ['tree', 'input']:
                    self.assertEqual(table.find_batch('name', queries, order=order), expected)


if __name__ == '__main__':
    unittest.main()
