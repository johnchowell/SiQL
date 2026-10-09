"""Independent tree lookups versus batched query scheduling and chained skip-list fingers.

Run: python benchmarks/batch.py [--quick | --replot]
"""
import json
import platform
import random
import statistics
import string
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from crud import OUT, bar_chart
from siql import Table, TableDiff, __version__
from siql.models import AddRow

MODES = ['Independent', 'Input chain', 'Tree order: fresh', 'Tree order: chain']


def compare(table, column, queries, repeats, batches_per_pass):
    expected = [table.find(column, value) for value in queries]
    calls = [lambda: [table.find(column, value) for value in queries],
             lambda: table.find_batch(column, queries, order='input'),
             lambda: table.find_batch(column, queries, order='tree', chained=False),
             lambda: table.find_batch(column, queries, order='tree')]
    measurements = {mode: [] for mode in MODES}
    for call in calls:
        if call() != expected:
            raise AssertionError('batch results differ from independent exact lookup')
    for repeat in range(repeats):
        # Alternate timing order; include scheduling and result restoration.
        ordering = list(range(len(calls)))
        random.Random(repeat).shuffle(ordering)
        for i in ordering:
            start = time.perf_counter_ns()
            for _ in range(batches_per_pass):
                result = calls[i]()
            elapsed = (time.perf_counter_ns() - start) / 1000 / len(queries) / batches_per_pass
            if result != expected:
                raise AssertionError('batch results differ from independent exact lookup')
            measurements[MODES[i]].append(elapsed)
    return {'latency_us': {mode: statistics.median(runs) for mode, runs in measurements.items()},
            'runs_us': measurements, 'exact_agreement': 1.0, 'queries': len(queries)}


def run(size, query_count, repeats, batch_sizes, batches_per_pass):
    rng = random.Random(43)
    names = [''.join(rng.choices(string.ascii_letters, k=8)) for _ in range(size)]
    with tempfile.TemporaryDirectory() as directory:
        table = Table(file=str(Path(directory) / 'rows.siql'))
        table.addCols(name=str, n=int)
        TableDiff([AddRow(i, {'name': name, 'n': i}) for i, name in enumerate(names)]).apply(table)
        table.find('name', names[0])  # Build the shared position map outside the timed region.
        shuffled = [names[i] for i in rng.sample(range(size), query_count)]
        begin = 200 if size == 1000 else 1200 if size == 10000 else 12000
        neighbors = list(range(begin, begin + query_count))
        rng.shuffle(neighbors)
        pool = rng.sample(names, 8)
        duplicates = [rng.choice(pool) for _ in range(query_count)]
        workloads = {}
        for label, column, queries in [('Random names', 'name', shuffled),
                                        ('Nearby numbers', 'n', neighbors),
                                        ('Repeated names', 'name', duplicates)]:
            workloads[label] = compare(table, column, queries, repeats, batches_per_pass)
            print(f'{size:,} rows, {label}: {workloads[label]["latency_us"]}', flush=True)
        scaling = {}
        if size == 50000:
            for count in batch_sizes:
                queries = [names[i] for i in rng.sample(range(size), count)]
                scaling[str(count)] = compare(table, 'name', queries, repeats, batches_per_pass)
        return {'workloads': workloads, 'scaling': scaling}


def charts(data):
    sizes = sorted(data['results'], key=int)
    labels = [f'{int(size):,} rows' for size in sizes]
    for workload, filename in [('Random names', 'batch_random.svg'),
                               ('Nearby numbers', 'batch_neighbors.svg')]:
        bar_chart(OUT / filename, f'Exact tree batches: {workload.lower()} (lower is better)', labels,
                  [(mode, [data['results'][size]['workloads'][workload]['latency_us'][mode]
                           for size in sizes]) for mode in MODES], 'microseconds per query', log=True)
    workloads = ['Random names', 'Nearby numbers', 'Repeated names']
    latest = data['results'][sizes[-1]]['workloads']
    bar_chart(OUT / 'batch_speedup.svg', f'Batch speed-up at {int(sizes[-1]):,} rows (higher is better)',
              workloads, [(mode, [latest[w]['latency_us'][MODES[0]] / latest[w]['latency_us'][mode]
                                  for w in workloads]) for mode in MODES[1:]], 'speed-up over independent',
              unit='x', ref=1)


def main():
    import sys
    path = OUT / 'batch_results.json'
    if '--replot' in sys.argv:
        charts(json.loads(path.read_text(encoding='utf-8')))
        return
    quick = '--quick' in sys.argv
    sizes = [1000] if quick else [1000, 10000, 50000]
    queries, repeats = (50, 3) if quick else (300, 9)
    batches_per_pass = 3 if quick else 20
    data = {'meta': {
        'siql': __version__, 'python': platform.python_version(),
        'os': f'{platform.system()} {platform.release()}', 'machine': platform.processor() or platform.machine(),
        'measured_at': datetime.now(timezone.utc).isoformat(), 'queries': queries, 'repeats': repeats,
        'batches_per_pass': batches_per_pass,
        'dataset': 'seed 43; random 8-letter names and sequential n; tree enabled',
        'timing': 'warm index; median microseconds/query; includes batching, sorting, and output restoration',
        'tree': 'ascending index keys; adaptive finger when q >= 8 and q * 64 >= branch cells, else fresh seeks',
        'neighbors': f'{queries} shuffled contiguous numeric values within one first-digit branch',
        'duplicates': f'{queries} names sampled with replacement from eight names',
        'scaling': 'random name queries; batch sizes 8, 32, 128, 512 at 50000 rows',
    }, 'results': {}}
    for size in sizes:
        data['results'][str(size)] = run(size, queries, repeats, [8, 32, 128, 512], batches_per_pass)
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')
    charts(data)


if __name__ == '__main__':
    main()
