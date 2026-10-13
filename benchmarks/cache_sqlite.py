"""Repeat/cold/fan-out lookup speeds and rolling-cache stress, compared with indexed SQLite.

Run: python benchmarks/cache_sqlite.py [--quick | --replot] [--no-plots] [--output DIRECTORY]
Writes cache_sqlite_results.json, cache_stress_results.json, and three SVG charts.
"""
import argparse
import gc
import json
import platform
import random
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from siql import Table, TableDiff, __version__
from siql.models import AddRow, DropRow, RenameCol, SetCell

MODES = ['SiQL tree', 'SiQL cache', 'SQLite memory', 'SQLite file, warm']
OUT = Path(__file__).resolve().parents[1] / 'docs' / 'benchmarks'


def sqlite_table(path, values):
    db = sqlite3.connect(path)
    db.execute('PRAGMA cache_size=-97656')  # Approximately 100 MB of pages, not a result cache.
    db.execute('CREATE TABLE rows (n INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL)')
    db.executemany('INSERT INTO rows VALUES (?, ?, ?)', values)
    db.execute('CREATE INDEX name_lookup ON rows(name)')
    db.execute('CREATE INDEX category_lookup ON rows(category)')
    db.commit()
    return db


def speed(size, queries, repeats):
    rng = random.Random(84)
    values = [(i, f'item {i:09}', f'group {i % 256:03}') for i in range(size)]
    with tempfile.TemporaryDirectory() as directory:
        table = Table(file=str(Path(directory) / 'rows.siql'), cache_max_bytes=0)
        table.addCols(n=int, name=str, category=str)
        TableDiff([AddRow(i, dict(n=n, name=name, category=category))
                   for i, (n, name, category) in enumerate(values)]).apply(table)
        memory = sqlite_table(':memory:', values)
        disk = sqlite_table(str(Path(directory) / 'rows.sqlite'), values)
        plans = {column: memory.execute(f'EXPLAIN QUERY PLAN SELECT n FROM rows WHERE {column}=? ORDER BY n',
                                       (values[0][1 if column == 'name' else 2],)).fetchall()
                 for column in ['name', 'category']}
        table.find('n', 0)  # Position-map construction is outside lookup timing.
        hot = [values[i][1] for i in rng.sample(range(size), 32)]
        traces = {
            'Repeated names': ('name', [rng.choice(hot) for _ in range(queries)], True),
            'Cold distinct names': ('name', [values[i][1] for i in rng.sample(range(size), min(size, queries))], False),
            'Repeated fan-out': ('category', [f'group {rng.randrange(16):03}' for _ in range(queries)], True),
        }
        results = {}
        for workload, (column, trace, warm) in traces.items():
            sql = f'SELECT n FROM rows WHERE {column}=? ORDER BY n'
            expected = [[row[0] for row in memory.execute(sql, (value,))] for value in trace]
            samples, infos = {mode: [] for mode in MODES}, {}
            for repeat in range(repeats):
                ordering = list(range(len(MODES)))
                random.Random(repeat).shuffle(ordering)
                for mode in ordering:
                    table.cache_max_bytes = 0 if mode == 0 else 100_000_000
                    table.clear_cache()
                    if mode < 2:
                        if warm:
                            for value in set(trace):
                                table.find(column, value)
                        start = time.perf_counter_ns()
                        actual = [table.find(column, value) for value in trace]
                        elapsed = time.perf_counter_ns() - start
                        infos[MODES[mode]] = table.cache_info
                    else:
                        db = memory if mode == 2 else disk
                        for value in set(trace):
                            db.execute(sql, (value,)).fetchall()  # Warm pages, index, and statement cache.
                        start = time.perf_counter_ns()
                        actual = [[row[0] for row in db.execute(sql, (value,))] for value in trace]
                        elapsed = time.perf_counter_ns() - start
                    if actual != expected:
                        raise AssertionError(f'{MODES[mode]} disagrees with SQLite for {workload}')
                    samples[MODES[mode]].append(elapsed / 1000 / len(trace))
            results[workload] = {
                'latency_us': {mode: statistics.median(runs) for mode, runs in samples.items()},
                'runs_us': samples, 'queries': len(trace), 'exact_agreement': 1.0,
                'mean_rows_returned': statistics.mean(map(len, expected)), 'cache_info': infos,
            }
            print(f'{size:,} rows, {workload}: {results[workload]["latency_us"]}', flush=True)
        memory.close()
        disk.close()
        return {'workloads': results, 'sqlite_query_plans': plans}


def mutation_stress(steps, *, traversed_nodes=False):
    rng = random.Random(85)
    size = 10000
    values = [(i, f'hot {i % 512:03}', 'group') for i in range(size)]
    with tempfile.TemporaryDirectory() as directory:
        table = Table(file=str(Path(directory) / 'rows.siql'), lazy_delete=True,
                      cache_traversed_nodes=traversed_nodes)
        table.addCols(n=int, name=str, category=str)
        TableDiff([AddRow(i, dict(n=n, name=name, category=category))
                   for i, (n, name, category) in enumerate(values)]).apply(table)
        oracle = sqlite_table(':memory:', values)
        column, next_id, checks = 'name', size, 0
        operations = {'read': 0, 'edit': 0, 'append': 0, 'delete': 0, 'resize': 0, 'rename': 0}
        maximum_bytes = 0
        start = time.perf_counter()
        for step in range(steps):
            if step % 137 == 0:
                table.cache_max_bytes = rng.choice([0, 2000, 10000, 1_000_000, 100_000_000])
                operations['resize'] += 1
            if step and step % 997 == 0:
                new = 'label' if column == 'name' else 'name'
                TableDiff([RenameCol(column, new)]).apply(table)
                oracle.execute(f'ALTER TABLE rows RENAME COLUMN {column} TO {new}')
                column = new
                operations['rename'] += 1
            kind = rng.randrange(10)
            value = f'hot {rng.randrange(600):03}'
            if kind == 0:
                index = rng.randrange(len(table.rows))
                identity = table.rows[index].col('n').value
                if step % 2:
                    table.rows[index].col(column).value = value
                else:
                    TableDiff([SetCell(index, column, value)]).apply(table)
                oracle.execute(f'UPDATE rows SET {column}=? WHERE n=?', (value, identity))
                operations['edit'] += 1
            elif kind == 1:
                table.add(**{'n': next_id, column: value, 'category': 'group'})
                oracle.execute(f'INSERT INTO rows(n,{column},category) VALUES (?,?,?)', (next_id, value, 'group'))
                next_id += 1
                operations['append'] += 1
            elif kind == 2:
                index = rng.randrange(len(table.rows))
                identity = table.rows[index].col('n').value
                TableDiff([DropRow(index)]).apply(table)
                oracle.execute('DELETE FROM rows WHERE n=?', (identity,))
                operations['delete'] += 1
            else:
                operations['read'] += 1
            trace = [value, 'hot 000', 'missing']
            actual = table.find_batch(column, trace)
            for query, positions in zip(trace, actual):
                identities = [table.rows[i].col('n').value for i in positions]
                expected = [row[0] for row in oracle.execute(f'SELECT n FROM rows WHERE {column}=? ORDER BY n',
                                                            (query,))]
                if identities != expected:
                    raise AssertionError(f'stale cache at mutation step {step}, query {query}')
                checks += 1
            info = table.cache_info
            if info['bytes'] > info['max_bytes']:
                raise AssertionError('mutation workload exceeded cache budget')
            maximum_bytes = max(maximum_bytes, info['bytes'])
            if (step + 1) % 2000 == 0:
                print(f'Mutation stress: {step + 1:,} steps, {checks:,} SQLite comparisons passed', flush=True)
        elapsed = time.perf_counter() - start
        table.compact()  # Include direct cell edits in the reload check.
        loaded = Table(file=table._file)
        rows = [(r.col('n').value, r.col(column).value) for r in loaded.rows]
        if rows != oracle.execute(f'SELECT n,{column} FROM rows ORDER BY n').fetchall():
            raise AssertionError('reload disagrees with SQLite')
        oracle.close()
        return {'steps': steps, 'query_comparisons': checks, 'mismatches': 0, 'operations': operations,
                'rows_remaining': len(table.rows), 'elapsed_seconds': elapsed,
                'maximum_accounted_bytes': maximum_bytes, 'final_cache_info': table.cache_info,
                'reload_agreement': True, 'persisted_operations_timed_for_stress_only': True}


def memory_stress(fill, churn):
    # Empty-table misses exercise cache overhead and keys without table/SQLite allocations in the trace.
    with tempfile.TemporaryDirectory() as directory:
        table = Table(file=str(Path(directory) / 'empty.siql'))
        table.addCols(name=str)
        snapshots = []
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        start = time.perf_counter()

        def snapshot(label, count):
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            info = table.cache_info
            if info['bytes'] > info['max_bytes']:
                raise AssertionError('retained cache exceeded byte limit')
            snapshots.append({'stage': label, 'queries': count, 'cache_info': info,
                              'traced_growth_bytes': max(0, current - baseline), 'traced_peak_bytes': peak})
            print(f'Memory stress {label}: {info}, traced growth {max(0, current - baseline):,} bytes', flush=True)

        for i in range(fill):
            if table.find('name', f'missing {i:09}'):
                raise AssertionError('an empty table returned rows')
        snapshot('fill 100 MB budget', fill)
        for cycle in range(2):
            for i in range(churn):
                table.find('name', f'churn {cycle} {i:09}')
            snapshot(f'churn {cycle + 1}', churn)
        table.cache_max_bytes = 1_000_000
        snapshot('shrink to 1 MB', 0)
        for i in range(churn):
            table.find('name', f'small {i:09}')
        snapshot('churn at 1 MB', churn)
        table.cache_max_bytes = 0
        snapshot('disable', 0)
        elapsed = time.perf_counter() - start
        tracemalloc.stop()
        return {'queries': fill + churn * 3, 'elapsed_seconds': elapsed, 'snapshots': snapshots,
                'note': 'tracemalloc Python allocations, not RSS; rows already resident are excluded; tracing affects timing'}


def charts(data, stress):
    from crud import bar_chart
    sizes = sorted(data['results'], key=int)
    labels = [f'{int(s):,} rows' for s in sizes]
    for workload, filename in [('Repeated names', 'cache_sqlite_hot.svg'),
                               ('Cold distinct names', 'cache_sqlite_cold.svg')]:
        title = 'distinct result-cache keys' if workload == 'Cold distinct names' else workload.lower()
        bar_chart(OUT / filename, f'Indexed lookup: {title} (lower is better)', labels,
                  [(mode, [data['results'][s]['workloads'][workload]['latency_us'][mode] for s in sizes])
                   for mode in MODES], 'microseconds per query', log=True)
    snapshots = stress['memory']['snapshots']
    bar_chart(OUT / 'cache_stress_memory.svg', 'Rolling-cache retained storage under pressure',
              [s['stage'] for s in snapshots],
              [('Byte budget', [s['cache_info']['max_bytes'] / 1e6 for s in snapshots]),
               ('Accounted cache', [s['cache_info']['bytes'] / 1e6 for s in snapshots]),
               ('Traced allocation growth', [s['traced_growth_bytes'] / 1e6 for s in snapshots])],
              'MB (decimal)', unit='MB', figsize=(12, 4.5))


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--replot', action='store_true')
    parser.add_argument('--no-plots', action='store_true')
    parser.add_argument('--output', type=Path, default=OUT)
    args = parser.parse_args()
    OUT = args.output.resolve()
    speed_path, stress_path = OUT / 'cache_sqlite_results.json', OUT / 'cache_stress_results.json'
    if args.replot:
        charts(json.loads(speed_path.read_text()), json.loads(stress_path.read_text()))
        return
    quick = args.quick
    meta = {'siql': __version__, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'os': platform.platform(), 'measured_at': datetime.now(timezone.utc).isoformat(),
            'timing': 'median end-to-end us/query; includes result materialization; fixture and cache warmup excluded',
            'sqlite_settings': 'indexed covering equality lookup; default statement cache; cache_size=-97656',
            'sqlite_file': 'warm OS/SQLite pages; reads only; no cold-disk or durable-write speed claim',
            'cache': '100000000 bytes; clear between runs; prewarm for repeated cases; cold trace has distinct keys',
            'cache_info_note': 'entry/byte counts are snapshots; hit/miss/eviction counters accumulate across modes and repeats',
            'queries': 300 if quick else 3000, 'repeats': 3 if quick else 9,
            'seed': 84, 'result': 'row IDs, ascending; SiQL row positions equal IDs in immutable speed fixtures'}
    data = {'meta': meta, 'results': {}}
    for size in ([1000, 5000] if quick else [1000, 10000, 50000, 100000]):
        data['results'][str(size)] = speed(size, meta['queries'], meta['repeats'])
    stress = {'meta': meta, 'mutations': mutation_stress(1000 if quick else 10000),
              'memory': memory_stress(5000 if quick else 220000, 3000 if quick else 100000)}
    OUT.mkdir(parents=True, exist_ok=True)
    speed_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')
    stress_path.write_text(json.dumps(stress, indent=2) + '\n', encoding='utf-8', newline='\n')
    if not args.no_plots:
        charts(data, stress)


if __name__ == '__main__':
    main()
