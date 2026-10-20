"""Correctness boundaries for serialization and retained row-position offsets."""
import ast
import math
import random
import tempfile
import unittest
from pathlib import Path

from siql import Table, TableDiff
from siql.models import AddRow, DropRow, SetCell
from siql.models.diff import commit_line


class CrudOptimizations(unittest.TestCase):
    def test_literal_validation_preserves_fallbacks(self):
        class LiteralRepr:
            def __repr__(self):
                return "{'custom': [1, 2]}"

        class IntRepr(int):
            def __repr__(self):
                return '123'

        class PretendsBuiltin(type):
            def __eq__(self, other):
                return True

        class BadRepr(metaclass=PretendsBuiltin):
            def __repr__(self):
                return 'not a literal'

        values = [None, True, 10**100, -0.0, 1e300, 1 + 2j, 'x\n\x00😀', b'\x00\xff',
                  {'a': [1, (2,)], 'b': {1, 2}}, set(), LiteralRepr(), IntRepr(4)]
        for value in values:
            with self.subTest(value=value):
                op = SetCell(0, 'value', value)
                expected = repr(op.to_tuple()) + '\n'
                self.assertEqual(op.dumps(), expected)
                self.assertEqual(commit_line([op, op]), repr([op.to_tuple(), op.to_tuple()]) + '\n')
                self.assertEqual(ast.literal_eval(op.dumps()), ast.literal_eval(expected))
        cyclic = []
        cyclic.append(cyclic)
        # literal_eval historically accepts the ellipsis in a cyclic list's repr.
        # Keep that fallback behavior; do not silently tighten accepted values.
        self.assertEqual(SetCell(0, 'value', cyclic).dumps(), repr(('~', 0, 'value', cyclic)) + '\n')
        for value in [math.nan, math.inf, complex(0, math.inf), object(), BadRepr()]:
            with self.assertRaises(TypeError):
                SetCell(0, 'value', value).dumps()
        deep = 0
        for _ in range(220):
            deep = [deep]
        with self.assertRaises(TypeError):
            SetCell(0, 'value', deep).dumps()

    def test_eager_delete_offsets_rebuild_append_and_rollback(self):
        for cache_bytes in (0, 100_000_000):
            with self.subTest(cache_bytes=cache_bytes), tempfile.TemporaryDirectory() as temp:
                path = str(Path(temp) / 'table.siql')
                table = Table(file=path, cache_max_bytes=cache_bytes)
                table.addCols(n=int)
                expected = list(range(600))
                TableDiff([AddRow(i, {'n': i}) for i in expected]).apply(table)
                table.find('n', 0)
                original_map = table._positions
                TableDiff([DropRow(10)]).apply(table)
                expected.pop(10)
                self.assertIs(table._positions, original_map)
                self.assertFalse(table.index['n'].dead)  # Eager removal stays eager.
                rng = random.Random(42)
                for step in range(450):
                    index = rng.randrange(len(expected))
                    TableDiff([DropRow(index)]).apply(table)
                    expected.pop(index)
                    if step % 7 == 0:
                        table.add(n=1000 + step)
                        expected.append(1000 + step)
                    if step % 13 == 0:
                        before = Path(path).read_bytes()
                        with self.assertRaises(IndexError):
                            TableDiff([DropRow(-1), AddRow(0, {'absent': 1})]).apply(table)
                        self.assertEqual(Path(path).read_bytes(), before)
                    probes = rng.sample(expected, min(5, len(expected)))
                    self.assertEqual(table.find_batch('n', probes), [[expected.index(n)] for n in probes])
                TableDiff([AddRow(1, {'n': -1})]).apply(table)
                expected.insert(1, -1)
                self.assertEqual(table.find_batch('n', expected), [[i] for i in range(len(expected))])
                reopened = Table(file=path)
                self.assertEqual([r.col('n').value for r in reopened.rows], expected)
