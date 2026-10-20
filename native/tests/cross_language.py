"""Shared scalar CRUD and file checks for the independent native core."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from siql.native import NativeTable  # noqa: E402
from siql import Table  # noqa: E402


class NativeCoreCompatibility(unittest.TestCase):
    def test_python_file_read_and_native_mutations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / '😀-shared.siql')
            python = Table(file=path)
            python.addCols(name=str, n=int, note=bytes)
            python.add(name="An'na", n=1, note=b'\x00\xff')
            python.add(name='😀', n=2**53 + 1, note=b'hi')
            native = NativeTable(path)
            self.assertEqual(native.names, ['name', 'n', 'note'])
            self.assertEqual(native.get(0, 'note'), b'\x00\xff')
            self.assertEqual(native.find('n', 1.0), [0])
            self.assertEqual(native.find('n', True), [0])
            self.assertEqual(native.find('n', float(2**53)), [])
            self.assertEqual(native.find('n', 2**53 + 1), [1])
            native.set(0, 'name', 'Alice')
            native.delete(1)
            native.append(name='NUL\x00line\nend', n=-4, note=b'\x00x')
            native.close()
            replayed = Table(file=path)
            self.assertEqual(replayed.find('name', 'Alice'), [0])
            self.assertEqual(replayed.find('n', -4), [1])
            self.assertEqual(replayed.rows[1].col('name').value, 'NUL\x00line\nend')
            self.assertEqual(replayed.rows[1].col('note').value, b'\x00x')

    def test_native_file_reopens_in_both_languages(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'native.siql')
            native = NativeTable(path)
            native.add_column('name', 'str')
            native.add_column('value', 'int')
            native.append_batch([{'name': f'item{i}', 'value': i} for i in range(150)])
            self.assertEqual(native.find('name', 'item149'), [149])
            native.set_tree(False)
            self.assertEqual(native.find('value', 149), [149])
            native.set_tree(True)
            native.delete(70)
            self.assertEqual(native.find('value', 149), [148])
            native.close()
            replayed = Table(file=path)
            self.assertEqual(len(replayed.rows), 149)
            self.assertEqual(replayed.find('value', 149), [148])
            self.assertEqual(NativeTable(path).find('name', 'item149'), [148])

    def test_unsupported_value_rejected_without_silent_reinterpretation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'unsupported.siql')
            native = NativeTable(path)
            native.add_column('payload', 'object')
            with self.assertRaises(TypeError):
                native.append(payload={'nested': [1]})
            native.close()
            python = Table(file=path)
            python.add(payload={'nested': [1]})
            with self.assertRaisesRegex(OSError, 'does not yet support container cells'):
                NativeTable(path)
            self.assertEqual(len(python.rows), 1)

    def test_unicode_escape_replay_and_unsupported_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'escaped.siql'
            path.write_text("('+c', 'text', 'str', 0)\n('+r', 0, {'text': '\\xff\\a'})\n",
                            encoding='utf-8')
            native = NativeTable(path)
            self.assertEqual(native.get(0, 'text'), '\xff\a')
            self.assertEqual(native.get(0, 'text'), Table(file=str(path)).rows[0].col('text').value)
            native.close()
            path.write_text("('+c', 'text', 'str', 0)\n('+r', 0, {'text': '\\N{LATIN SMALL LETTER A}'})\n",
                            encoding='utf-8')
            with self.assertRaisesRegex(OSError, 'unsupported string escape'):
                NativeTable(path)

    def test_rolling_cache_preserves_live_positions(self):
        with tempfile.TemporaryDirectory() as folder:
            native = NativeTable(Path(folder) / 'cache.siql')
            self.assertEqual(native.cache_info.max_bytes, 100_000_000)
            native.add_column('name', 'str')
            native.add_column('n', 'int')
            native.append(name='Ann', n=1)
            native.append(name='Bob', n=2)
            self.assertEqual(native.find('name', 'Bob'), [1])
            self.assertEqual(native.find('name', 'Bob'), [1])
            self.assertGreater(native.cache_info.hits, 0)
            hits = native.cache_info.hits
            native.set(1, 'n', 3)
            self.assertEqual(native.find('name', 'Bob'), [1])
            self.assertEqual(native.cache_info.hits, hits + 1)
            native.delete(0)
            self.assertEqual(native.find('name', 'Bob'), [0])
            self.assertGreater(native.cache_info.invalidations, 0)
            native.cache_max_bytes = 0
            self.assertEqual(native.cache_info.entries, 0)
            native.cache_max_bytes = 1000
            self.assertEqual(native.find('name', 'Bob'), [0])
            self.assertLessEqual(native.cache_info.bytes, 1000)
            self.assertEqual(native.find('name', 'Ann'), [])
            self.assertGreater(native.cache_info.evictions, 0)
            misses = native.cache_info.misses
            self.assertEqual(native.find('name', 'Bob'), [0])
            self.assertEqual(native.cache_info.misses, misses + 1)
            native.clear_cache()
            self.assertEqual(native.cache_info.entries, 0)
            native.close()

    def test_nonfinite_float_is_rejected_before_logging(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'finite.siql'
            native = NativeTable(path)
            native.add_column('value', 'float')
            with self.assertRaisesRegex(RuntimeError, 'nonfinite'):
                native.append(value=float('nan'))
            with self.assertRaisesRegex(RuntimeError, 'nonfinite'):
                native.append(value=float('inf'))
            self.assertEqual(len(native), 0)
            native.close()
            self.assertEqual(len(Table(file=str(path)).rows), 0)

    def test_find_more_than_small_result_buffer(self):
        with tempfile.TemporaryDirectory() as folder:
            native = NativeTable(Path(folder) / 'many.siql')
            native.add_column('group', 'str')
            for _ in range(20):
                native.append(group='same')
            self.assertEqual(native.find('group', 'same'), list(range(20)))
            native.close()


if __name__ == '__main__':
    unittest.main()
