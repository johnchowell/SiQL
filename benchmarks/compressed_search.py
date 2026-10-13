"""All-column prefix grouping versus a tree followed by exact scans of narrowed rows.

Run: python benchmarks/compressed_search.py [--quick | --replot]
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

from siql import Table, TableDiff
from siql.models import AddRow

OUT = Path(__file__).resolve().parents[1] / 'docs/benchmarks'


def grouped_find(table, criteria, positions):
    if len(criteria) > 1:
        return table.find_where(**criteria)
    # Preserve the prototype comparison: public single-column searches bypass grouping.
    return sorted(positions[id(row)] for row in table._compressed_index.find(criteria))


def run(size, queries, repeats):
    rng = random.Random(88)
    names = [chr(65 + i % 26) + f' person {i:03}' for i in range(512)]
    values = [{'name': rng.choice(names), 'age': rng.randrange(100),
               'blob': bytes([rng.randrange(16), rng.randrange(8)])} for _ in range(size)]
    selected = rng.sample(range(size), queries)
    traces = {
        'One column': [{'name': values[i]['name']} for i in selected],
        'Two columns': [{k: values[i][k] for k in ['name', 'age']} for i in selected],
        'All three columns': [values[i] for i in selected],
    }
    with tempfile.TemporaryDirectory() as directory:
        table = Table(file=str(Path(directory) / 'rows.siql'), cache_max_bytes=0)
        table.addCols(name=str, age=int, blob=bytes)
        TableDiff([AddRow(i, record) for i, record in enumerate(values)]).apply(table)
        table.find('name', values[0]['name'])
        baseline = {}
        for label, trace in traces.items():
            expected = [table.find_where(**criteria) for criteria in trace]
            runs = []
            for _ in range(repeats):
                start = time.perf_counter_ns()
                actual = [table.find_where(**criteria) for criteria in trace]
                runs.append((time.perf_counter_ns() - start) / 1000 / len(trace))
                if actual != expected:
                    raise AssertionError('baseline is inconsistent')
            baseline[label] = {'latency_us': statistics.median(runs), 'runs_us': runs,
                               'expected': expected}
        start = time.perf_counter()
        table.compressed_search = True
        build_ms = (time.perf_counter() - start) * 1000
        positions = {id(row): i for i, row in enumerate(table.rows)}
        results = {}
        for label, trace in traces.items():
            start = time.perf_counter_ns()
            actual, candidates, groups = [], [], []
            for criteria in trace:
                actual.append(grouped_find(table, criteria, positions))
                # Cache is disabled; these counters describe the actual prefix prefilter.
                candidates.append(table._compressed_index.last_candidates)
                groups.append(table._compressed_index.last_groups)
            cold_us = (time.perf_counter_ns() - start) / 1000 / len(trace)
            if actual != baseline[label]['expected']:
                raise AssertionError('compressed lookup lost an exact match')
            runs = []
            for _ in range(repeats):
                start = time.perf_counter_ns()
                actual = [grouped_find(table, criteria, positions) for criteria in trace]
                runs.append((time.perf_counter_ns() - start) / 1000 / len(trace))
                if actual != baseline[label]['expected']:
                    raise AssertionError('compressed lookup differs after warming')
            original = dict(baseline[label])
            del original['expected']
            results[label] = {'baseline': original, 'compressed_warm_us': statistics.median(runs),
                              'compressed_runs_us': runs, 'compressed_first_pass_us': cold_us,
                              'candidate_rows_mean': statistics.mean(candidates),
                              'candidate_groups_mean': statistics.mean(groups), 'exact_agreement': 1.0}
            print(f'{size:,} rows, {label}: {results[label]}', flush=True)
        return {'workloads': results, 'group_build_ms': build_ms, 'index_info': table.compressed_info}


def charts(data):
    from crud import bar_chart
    largest = str(max(map(int, data['results'])))
    results = data['results'][largest]['workloads']
    labels = list(results)
    bar_chart(OUT / 'compressed_search.svg', f'Prefix-group exact search at {int(largest):,} rows (lower is better)',
              labels, [('Tree + narrowed scan', [results[w]['baseline']['latency_us'] for w in labels]),
                       ('Groups, first pass', [results[w]['compressed_first_pass_us'] for w in labels]),
                       ('Groups, warm trees', [results[w]['compressed_warm_us'] for w in labels])],
              'microseconds per query', log=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--replot', action='store_true')
    args = parser.parse_args()
    path = OUT / 'compressed_search_results.json'
    if args.replot:
        charts(json.loads(path.read_text(encoding='utf-8')))
        return
    sizes = [1000] if args.quick else [1000, 10000, 100000]
    queries, repeats = (50, 3) if args.quick else (300, 9)
    data = {'meta': {'python': platform.python_version(), 'os': platform.platform(),
                     'measured_at': datetime.now(timezone.utc).isoformat(), 'queries': queries, 'repeats': repeats,
                     'prefixes': 'first Unicode character; first decimal digit; first byte',
                     'signatures': 'interned little-endian uint32 symbol per column, shared within each group',
                     'cache_max_bytes': 0, 'seed': 88,
                     'single_column_note': 'forced grouped prototype; public single-column lookups use the ordinary tree',
                     'baseline': 'first predicate by coarse tree selectivity; existing exact scan for remaining predicates',
                     'timing': 'fixtures and group formation excluded; first pass includes lazy group-tree creation',
                     'first_pass_note': 'workloads run sequentially; later first passes may reuse trees built by earlier workloads'},
            'results': {str(size): run(size, queries, repeats) for size in sizes}}
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
