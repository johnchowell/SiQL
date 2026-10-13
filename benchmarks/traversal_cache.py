"""Measure caching all inspected node values against result-only caching, using the same tree.

Run: python benchmarks/traversal_cache.py [--quick | --replot] [--output JSON_PATH]
"""
import argparse
import json
import platform
import random
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from cache_sqlite import mutation_stress
from siql import Table, TableDiff
from siql.models import AddRow

MODES = {'Tree only': (0, False), 'Results only': (100_000_000, False),
         'All inspected nodes': (100_000_000, True)}


def run(size, queries, repeats):
    rng = random.Random(86)
    names = [f'item {i:09}' for i in range(size)]
    hot = rng.sample(names, 32)
    traces = {'Random distinct': rng.sample(names, queries),
              'Nearby distinct': names[size // 2:size // 2 + queries],
              'Repeated names': [rng.choice(hot) for _ in range(queries)]}
    with tempfile.TemporaryDirectory() as directory:
        table = Table(file=str(Path(directory) / 'rows.siql'), cache_max_bytes=0)
        table.addCols(name=str)
        TableDiff([AddRow(i, {'name': name}) for i, name in enumerate(names)]).apply(table)
        table.find('name', names[0])
        out = {}
        for workload, trace in traces.items():
            expected = [[int(value.split()[1])] for value in trace]
            records = {mode: [] for mode in MODES}
            for repeat in range(repeats):
                ordering = list(MODES)
                random.Random(repeat).shuffle(ordering)
                for mode in ordering:
                    budget, passed = MODES[mode]
                    table.clear_cache()
                    table.cache_max_bytes = budget
                    table.cache_traversed_nodes = passed
                    before = table.cache_info
                    start = time.perf_counter_ns()
                    actual = [table.find('name', value) for value in trace]
                    elapsed = time.perf_counter_ns() - start
                    if actual != expected:
                        raise AssertionError(f'{mode}: mismatched result')
                    info = table.cache_info
                    if info['bytes'] > budget:
                        raise AssertionError('cache budget exceeded')
                    attempts = info['hits'] + info['misses'] - before['hits'] - before['misses']
                    records[mode].append({
                        'latency_us': elapsed / 1000 / len(trace), 'retained_bytes': info['bytes'],
                        'entries': info['entries'], 'hits': info['hits'] - before['hits'],
                        'hit_rate': (info['hits'] - before['hits']) / attempts if attempts else 0,
                        'nodes_seen': info['traversal_nodes_seen'] - before['traversal_nodes_seen'],
                        'node_admissions': info['traversal_admissions'] - before['traversal_admissions'],
                    })
            summary = {mode: {key: statistics.median(r[key] for r in runs) for key in runs[0]}
                       for mode, runs in records.items()}
            out[workload] = {'modes': summary, 'runs': records, 'exact_agreement': 1.0}
            print(f'{size:,} rows, {workload}: {summary}', flush=True)
        return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--replot', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('docs/benchmarks/traversal_cache_results.json'))
    args = parser.parse_args()
    if args.replot:
        from crud import bar_chart
        data = json.loads(args.output.read_text(encoding='utf-8'))
        workloads = list(data['results']['100000'])
        bar_chart(args.output.with_name('traversal_cache.svg'),
                  'Caching inspected values: 100,000 rows (lower is better)', workloads,
                  [(mode, [data['results']['100000'][w]['modes'][mode]['latency_us'] for w in workloads])
                   for mode in MODES], 'microseconds per query', log=True)
        return
    sizes = [1000] if args.quick else [1000, 10000, 100000]
    queries, repeats = (300, 3) if args.quick else (1000, 9)
    data = {'meta': {'python': platform.python_version(), 'os': platform.platform(),
                     'measured_at': datetime.now(timezone.utc).isoformat(), 'queries': queries, 'repeats': repeats,
                     'timing': 'same live tree; cache empty before each mode; includes admission and result conversion',
                     'nodes': 'every unique compared node, including stopping successors; no skipped ranges expanded',
                     'budget': 'one shared 100000000-byte LRU; results and inspected-node values compete'},
            'results': {str(size): run(size, min(queries, size // 2), repeats) for size in sizes},
            'stress': mutation_stress(1000 if args.quick else 10000, traversed_nodes=True)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
