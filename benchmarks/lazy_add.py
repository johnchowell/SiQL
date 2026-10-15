"""Measure append latency and deferred read/flush work on the same append workload."""
import argparse
import json
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from siql import Table, TableDiff
from siql.models import AddRow

OUT = Path(__file__).resolve().parents[1] / 'docs/benchmarks'


def run(size, additions, repeats):
    results = {}
    for lazy in (False, True):
        runs = []
        for _ in range(repeats):
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / 'rows.siql')
                table = Table(file=path, cache_max_bytes=0)
                table.addCols(name=str, age=int)
                TableDiff([AddRow(i, {'name': f'A{i:09}', 'age': i % 100}) for i in range(size)]).apply(table)
                table.lazy_add = lazy
                diff = TableDiff([AddRow(size + i, {'name': f'A{size+i:09}', 'age': i % 100})
                                  for i in range(additions)])
                start = time.perf_counter_ns()
                diff.apply(table)
                appended = time.perf_counter_ns()
                if table.find('name', f'A{size:09}') != [size]:
                    raise AssertionError('new row is not immediately searchable')
                searched = time.perf_counter_ns()
                table.purge()
                flushed = time.perf_counter_ns()
                if any(node.pending or node.dead for node in table.index.values()):
                    raise AssertionError('flush left pending index work')
                runs.append({'Append': (appended - start) / 1e6, 'First read': (searched - appended) / 1e6,
                             'Flush': (flushed - searched) / 1e6, 'Total': (flushed - start) / 1e6})
        results['Lazy add' if lazy else 'Eager add'] = {
            'median_ms': {phase: statistics.median(run[phase] for run in runs) for phase in runs[0]},
            'runs_ms': runs}
    return results


def charts(data):
    from crud import bar_chart
    phases = ['Append', 'First read', 'Flush', 'Total']
    bar_chart(OUT / 'lazy_add.svg', f"Append {data['meta']['additions']:,} rows: eager versus lazy (lower is better)",
              phases, [(mode, [result['median_ms'][phase] for phase in phases])
                       for mode, result in data['results'].items()], 'milliseconds', unit='ms', log=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--replot', action='store_true')
    args = parser.parse_args()
    path = OUT / 'lazy_add_results.json'
    if args.replot:
        charts(json.loads(path.read_text(encoding='utf-8')))
        return
    size, additions, repeats = (1000, 100, 3) if args.quick else (50000, 5000, 7)
    data = {'meta': {'python': platform.python_version(), 'platform': platform.platform(),
                     'measured_at': datetime.now(timezone.utc).isoformat(), 'rows': size,
                     'additions': additions, 'repeats': repeats, 'cache_max_bytes': 0,
                     'timing': 'bulk append includes serialization and saving; fixture/diff creation excluded',
                     'order': 'eager then lazy; separate fresh table each repeat; includes first position-map build',
                     'flush': 'purge finishes remaining index work; total includes append, first read, and purge'},
            'results': run(size, additions, repeats)}
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps(data, indent=2), flush=True)


if __name__ == '__main__':
    main()
