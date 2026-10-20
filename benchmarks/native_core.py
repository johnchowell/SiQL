"""Compare the independent scalar C++ core with the current Python API and indexed SQLite.

The C++ arm calls its C ABI through the thin ctypes adapter. This benchmark covers
only two scalar columns and should not be used as a whole-library performance claim.
"""
import argparse
import ctypes
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from siql.native import NativeTable, Value, encode  # noqa: E402
from siql import Table, TableDiff  # noqa: E402
from siql.models import AddRow, DropRow  # noqa: E402

OPERATIONS = ['Create', 'Read (str)', 'Read (int)', 'Update', 'Delete + lookup', 'Batch create']
LABELS = {'cpp': 'Native C++ via ctypes', 'python': 'Python/Cython', 'sqlite': 'Indexed SQLite'}


def one(mode, size, count, folder):
    names = [f'name{i:08d}' for i in range(size)]
    probes = [(i * 7919) % size for i in range(count)]
    times = {}

    def timed(label, units, operation):
        start = time.perf_counter()
        operation()
        times[label] = (time.perf_counter() - start) * 1e6 / units

    if mode == 'cpp':
        table = NativeTable(folder / 'main.siql', cache_max_bytes=0)
        table.add_column('name', 'str')
        table.add_column('n', 'int')
        timed('Create', size, lambda: [table.append(name=name, n=i) for i, name in enumerate(names)])
        table.find('n', 0)  # position map is intentionally warm for read phases
        timed('Read (str)', count, lambda: [table.find('name', names[i]) for i in probes])
        timed('Read (int)', count, lambda: [table.find('n', i) for i in probes])
        def update():
            for i in probes:
                found = table.find('name', names[i])
                assert len(found) == 1
                table.set(found[0], 'name', names[i] + '!')
        timed('Update', count, update)
        def delete():
            for i in probes:
                found = table.find('name', names[i] + '!')
                assert len(found) == 1
                table.delete(found[0])
        timed('Delete + lookup', count, delete)
        remaining = [i for i in range(size) if i not in set(probes)]
        assert len(table) == len(remaining)
        assert [table.get(i, 'n') for i in range(len(table))] == remaining
        table.close()
        assert [r.col('n').value for r in Table(file=str(folder / 'main.siql'), tree=False).rows] == remaining
        bulk = NativeTable(folder / 'batch.siql', cache_max_bytes=0)
        bulk.add_column('name', 'str')
        bulk.add_column('n', 'int')
        payload = [encode(v) for i, name in enumerate(names) for v in (name, i)]
        encoded = (Value * len(payload))(*payload)
        timed('Batch create', size, lambda: bulk._check(bulk.lib.siql_append_batch(
            bulk.handle, encoded, size, 2)))
        assert len(bulk) == size
        bulk.close()
    elif mode == 'python':
        table = Table(file=str(folder / 'main.siql'), cache_max_bytes=0)
        table.addCols(name=str, n=int)
        timed('Create', size, lambda: [table.add(name=name, n=i) for i, name in enumerate(names)])
        table.find('n', 0)
        timed('Read (str)', count, lambda: [table.find('name', names[i]) for i in probes])
        timed('Read (int)', count, lambda: [table.find('n', i) for i in probes])
        timed('Update', count, lambda: [table.editByName('name', names[i], names[i] + '!') for i in probes])
        def delete():
            for i in probes:
                found = table.find('name', names[i] + '!')
                assert len(found) == 1
                TableDiff([DropRow(found[0])]).apply(table)
        timed('Delete + lookup', count, delete)
        remaining = [i for i in range(size) if i not in set(probes)]
        assert [r.col('n').value for r in table.rows] == remaining
        bulk = Table(file=str(folder / 'batch.siql'), cache_max_bytes=0)
        bulk.addCols(name=str, n=int)
        ops = [AddRow(i, {'name': name, 'n': i}) for i, name in enumerate(names)]
        timed('Batch create', size, lambda: TableDiff(ops).apply(bulk))
        assert len(bulk.rows) == size
    else:
        db = sqlite3.connect(folder / 'main.db', isolation_level=None)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=OFF')
        db.execute('CREATE TABLE t (name TEXT, n INTEGER)')
        db.execute('CREATE INDEX name_idx ON t(name)')
        db.execute('CREATE INDEX n_idx ON t(n)')
        timed('Create', size, lambda: [db.execute('INSERT INTO t VALUES (?, ?)', (name, i))
                                      for i, name in enumerate(names)])
        db.execute('SELECT rowid FROM t WHERE n = 0').fetchall()
        timed('Read (str)', count, lambda: [db.execute('SELECT rowid FROM t WHERE name = ?',
                                                       (names[i],)).fetchall() for i in probes])
        timed('Read (int)', count, lambda: [db.execute('SELECT rowid FROM t WHERE n = ?',
                                                       (i,)).fetchall() for i in probes])
        timed('Update', count, lambda: [db.execute('UPDATE t SET name = ? WHERE name = ?',
                                                  (names[i] + '!', names[i])) for i in probes])
        timed('Delete + lookup', count, lambda: [db.execute('DELETE FROM t WHERE name = ?',
                                                            (names[i] + '!',)) for i in probes])
        assert db.execute('SELECT COUNT(*) FROM t').fetchone()[0] == size - count
        db.close()
        bulk = sqlite3.connect(folder / 'batch.db', isolation_level=None)
        bulk.execute('PRAGMA journal_mode=WAL')
        bulk.execute('PRAGMA synchronous=OFF')
        bulk.execute('CREATE TABLE t (name TEXT, n INTEGER)')
        bulk.execute('CREATE INDEX name_idx ON t(name)')
        bulk.execute('CREATE INDEX n_idx ON t(n)')
        def batch():
            bulk.execute('BEGIN')
            bulk.executemany('INSERT INTO t VALUES (?, ?)', ((name, i) for i, name in enumerate(names)))
            bulk.execute('COMMIT')
        timed('Batch create', size, batch)
        assert bulk.execute('SELECT COUNT(*) FROM t').fetchone()[0] == size
        bulk.close()
    return times


def plot(data, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'svg.fonttype': 'none', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False})
    size = str(max(map(int, data['sizes'])))
    fig, ax = plt.subplots(figsize=(11, 5), layout='constrained')
    colors = ['#267da3', '#8996a5', '#e38b40']
    for series, mode in enumerate(LABELS):
        values = data['results'][size][mode]['median_us']
        offset = (series - 1) * .25
        bars = ax.barh([i + offset for i in range(len(OPERATIONS))],
                       [values[op] for op in OPERATIONS], height=.23,
                       color=colors[series], label=LABELS[mode])
        ax.bar_label(bars, [f'{values[op]:,.1f}' for op in OPERATIONS], padding=2, fontsize=8)
    ax.set_yticks(range(len(OPERATIONS)), OPERATIONS)
    ax.invert_yaxis()
    ax.set_xscale('log')
    ax.set_xlim(right=max(data['results'][size][mode]['median_us'][op]
                          for mode in LABELS for op in OPERATIONS) * 2.7)
    ax.set_xlabel('Microseconds per operation · log scale · lower is better')
    ax.set_title(f'Scalar CRUD comparison at {int(size):,} rows', fontweight='bold')
    ax.grid(axis='x', alpha=.2)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc='lower right')
    fig.savefig(output)
    plt.close(fig)
    output.write_text('\n'.join(line.rstrip() for line in output.read_text().splitlines()) + '\n')

    # Keep the README numbers tied to the data used for the graph.
    readme = ROOT / 'README.md'
    source = readme.read_text(encoding='utf-8')
    begin_marker, end_marker = '<!-- NATIVE_CORE_RESULTS_START -->', '<!-- NATIVE_CORE_RESULTS_END -->'
    begin = source.index(begin_marker) + len(begin_marker)
    end = source.index(end_marker)
    rows = ['| Operation | C++ via ctypes (µs) | Python/Cython (µs) | Indexed SQLite (µs) |',
            '|---|---:|---:|---:|']
    for op in OPERATIONS:
        rows.append('| ' + op + ' | ' + ' | '.join(
            f'{data["results"][size][mode]["median_us"][op]:,.1f}' for mode in LABELS) + ' |')
    source = source[:begin] + '\n\n' + '\n'.join(rows) + '\n\n' + source[end:]
    readme.write_text(source, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/benchmarks/native_core_results.json')
    parser.add_argument('--sizes', type=int, nargs='+', default=[1000, 10000, 50000])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--replot', action='store_true')
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args()
    if args.replot:
        data = json.loads(args.output.read_text())
        plot(data, args.output.with_suffix('.svg'))
        return
    data = {'measured_at': datetime.now(timezone.utc).isoformat(),
            'platform': platform.platform(), 'python': platform.python_version(),
            'native': 'independent C++ core via ctypes; scalar cells only',
            'sqlite': sqlite3.sqlite_version + ' / WAL / synchronous=OFF', 'sizes': args.sizes,
            'repeats': args.repeats, 'lookups': 500, 'cache_bytes': 0, 'results': {}}
    for mode in LABELS:
        with tempfile.TemporaryDirectory() as folder:
            one(mode, 300, 30, Path(folder))
    for size in args.sizes:
        data['results'][str(size)] = {}
        for mode in LABELS:
            runs = []
            for _ in range(args.repeats):
                with tempfile.TemporaryDirectory() as folder:
                    runs.append(one(mode, size, 500, Path(folder)))
            medians = {op: statistics.median(run[op] for run in runs) for op in OPERATIONS}
            data['results'][str(size)][mode] = {'runs_us': runs, 'median_us': medians}
            print(size, mode, medians, flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + '\n')
    if not args.no_plots:
        plot(data, args.output.with_suffix('.svg'))


if __name__ == '__main__':
    main()
