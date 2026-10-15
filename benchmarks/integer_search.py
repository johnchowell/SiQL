"""Full-value packed integer seeks versus the ordinary tree and prefix groups."""
import argparse
import json
import platform
import random
import statistics
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone

from siql import Table, TableDiff
from siql.models import AddRow

OUT = Path(__file__).resolve().parents[1] / 'docs/benchmarks'


def run(size, queries, repeats):
    rng = random.Random(98)
    values = [{'name': chr(65 + (n := rng.randrange(512)) % 26) + f' person {n:03}',
               'age': rng.randrange(100), 'blob': bytes([rng.randrange(16), rng.randrange(8)])}
              for _ in range(size)]
    selected = rng.sample(values, queries)
    traces = {label: [{column: row[column] for column in columns} for row in selected]
              for label, columns in [('Leading pair', ['name', 'age']), ('Hole in middle', ['name', 'blob']),
                                     ('Nonleading pair', ['age', 'blob']), ('All columns', ['name', 'age', 'blob'])]}
    with tempfile.TemporaryDirectory() as directory:
        table = Table(file=str(Path(directory) / 'rows.siql'), cache_max_bytes=0)
        table.addCols(name=str, age=int, blob=bytes)
        TableDiff([AddRow(i, row) for i, row in enumerate(values)]).apply(table)
        positions = {id(row): i for i, row in enumerate(table.rows)}
        expected = {label: [table.find_where(**criteria) for criteria in trace] for label, trace in traces.items()}
        results, formation = {}, {}
        for mode in ['Tree', 'Prefix groups', 'Integer under', 'Integer lower']:
            start = time.perf_counter()
            table.compressed_search = mode == 'Prefix groups'
            table.integer_search = mode.startswith('Integer')
            if mode.startswith('Integer'):
                table._compressed_index.ensure()
            formation[mode] = (time.perf_counter() - start) * 1000
            def lookup(criteria):
                if mode.startswith('Integer'):
                    seek = 'lower' if mode == 'Integer lower' else 'under'
                    return sorted(positions[id(row)] for row in table._compressed_index.find(criteria, seek=seek))
                return table.find_where(**criteria)
            results[mode] = {}
            for label, trace in traces.items():
                ranges, backwards = [], []
                start = time.perf_counter_ns()
                actual = []
                for criteria in trace:
                    actual.append(lookup(criteria))
                    if mode.startswith('Integer'):
                        ranges.append(table._compressed_index.last_range)
                        backwards.append(table._compressed_index.last_backtracked)
                first = (time.perf_counter_ns() - start) / 1000 / len(trace)
                if actual != expected[label]:
                    raise AssertionError((mode, label, 'first pass disagrees'))
                runs = []
                for _ in range(repeats):
                    start = time.perf_counter_ns()
                    actual = [lookup(criteria) for criteria in trace]
                    runs.append((time.perf_counter_ns() - start) / 1000 / len(trace))
                    if actual != expected[label]:
                        raise AssertionError((mode, label, 'warm pass disagrees'))
                results[mode][label] = {'warm_us': statistics.median(runs), 'runs_us': runs, 'first_pass_us': first,
                                       'mean_range_groups': statistics.mean(ranges) if ranges else None,
                                       'mean_backtracked_groups': statistics.mean(backwards) if backwards else None}
                print(size, mode, label, results[mode][label], flush=True)
        return {'workloads': results, 'formation_ms': formation, 'integer_info': table.integer_info}


def charts(data):
    from crud import bar_chart
    size = str(max(map(int, data['results'])))
    results = data['results'][size]['workloads']
    labels = list(results['Tree'])
    bar_chart(OUT / 'integer_search.svg', f'Full-row integer search at {int(size):,} rows (lower is better)',
              labels, [(mode, [workloads[label]['warm_us'] for label in labels])
                       for mode, workloads in results.items()], 'microseconds per query', log=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--replot', action='store_true')
    args = parser.parse_args()
    path = OUT / 'integer_search_results.json'
    if args.replot:
        charts(json.loads(path.read_text(encoding='utf-8')))
        return
    data = {'meta': {'python': platform.python_version(), 'platform': platform.platform(),
                     'measured_at': datetime.now(timezone.utc).isoformat(), 'seed': 98,
                     'queries': 30 if args.quick else 100, 'repeats': 3 if args.quick else 5,
                     'cache_max_bytes': 0, 'encoding': 'full canonical value bytes in length-prefixed padded slots',
                     'estimate': 'row-weighted mean encoded slot minus one quarter observed range',
                     'timing': 'formation and sorting excluded; warm passes follow one initial pass',
                     'note': 'integer lower reuses the under index; both use identical direct index calls and row-position conversion'},
            'results': {str(size): run(size, 30 if args.quick else 100, 3 if args.quick else 5)
                        for size in ([1000] if args.quick else [1000, 10000, 100000])}}
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
