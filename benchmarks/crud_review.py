"""Reproducible before/after native Python CRUD timings; no plotting dependency.

Run with --output PATH, then run again after rebuilding. All changes are saved
immediately. Cache is disabled to expose index costs. Fixture setup, verification,
and reopen checks are outside each measured phase.
"""
import argparse
import json
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from siql import Table, TableDiff
from siql.models import AddRow, DropRow
import siql.models.table as implementation


def run(size, count, folder):
    table = Table(file=str(folder / 'table.siql'), cache_max_bytes=0)
    table.addCols(name=str, n=int)
    names = [f'name{i:08d}' for i in range(size)]
    probes = [(i * 7919) % size for i in range(count)]
    times = {}

    def measure(label, count, operation):
        start = time.perf_counter()
        operation()
        times[label] = (time.perf_counter() - start) * 1e6 / count

    def create():
        for i, name in enumerate(names):
            table.add(name=name, n=i)

    def read(column, values):
        for value in values:
            table.find(column, value)

    def update():
        for i in probes:
            table.editByName('name', names[i], names[i] + '!')

    def delete():
        for i in probes:
            positions = table.find('name', names[i] + '!')
            assert len(positions) == 1
            TableDiff([DropRow(positions[0])]).apply(table)

    measure('Create', size, create)
    table.find('n', 0)  # Position-map construction is a separate one-time cost.
    measure('Read (str)', count, lambda: read('name', [names[i] for i in probes]))
    measure('Read (int)', count, lambda: read('n', probes))
    measure('Update', count, update)
    measure('Delete + lookup', count, delete)
    removed = set(probes)
    expected = [i for i in range(size) if i not in removed]
    assert [r.col('n').value for r in table.rows] == expected
    assert [r.col('n').value for r in Table(file=table._file, tree=False).rows] == expected
    batch = Table(file=str(folder / 'batch.siql'), cache_max_bytes=0)
    batch.addCols(name=str, n=int)
    ops = [AddRow(i, {'name': names[i], 'n': i}) for i in range(size)]
    measure('Batch create', size, lambda: TableDiff(ops).apply(batch))
    assert len(batch.rows) == size
    return times


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sizes', type=int, nargs='+', default=[10000, 50000])
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--queries', type=int, default=500)
    args = parser.parse_args()
    result = {'runtime': platform.python_version(), 'platform': platform.platform(),
              'implementation': implementation.__file__, 'utc': datetime.now(timezone.utc).isoformat(),
              'cache_bytes': 0, 'repeats': args.repeats, 'queries': args.queries, 'results': {}}
    with tempfile.TemporaryDirectory() as temp:
        run(300, 30, Path(temp))  # Warm imports and code paths.
    for size in args.sizes:
        runs = []
        for _ in range(args.repeats):
            with tempfile.TemporaryDirectory() as temp:
                runs.append(run(size, args.queries, Path(temp)))
        result['results'][str(size)] = {'runs_us': runs, 'median_us': {
            key: statistics.median(r[key] for r in runs) for key in runs[0]}}
        print(size, result['results'][str(size)]['median_us'], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
