"""Update README benchmark tables and conclusions from the refreshed JSON datasets."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

root = Path(__file__).resolve().parents[1]
data = json.loads((root / 'docs/benchmarks/results.json').read_text())
baseline = json.loads((root / 'docs/benchmarks/results-python.json').read_text())
results = data['results']
modes = ['off', 'on', 'lazy', 'sqlite', 'sqlite_index']
ops = ['Create', 'Read (str)', 'Read (int)', 'Update', 'Delete']
sizes = sorted(results, key=int)
big = sizes[-1]
current = results[big]
old = baseline['results'][big]
meta = data['meta']
pending = data['pending']['results']

def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |',
                      '|' + '---|' + '---:|' * (len(headers) - 1)] +
                     ['| ' + ' | '.join(row) + ' |' for row in rows])

crud = table(['Operation', 'Rows', 'Tree off (µs/op)', 'Tree on (µs/op)', 'Tree on + lazy delete (µs/op)',
              'SQLite (µs/op)', 'SQLite, indexed (µs/op)'],
             [[op, f'{int(s):,}'] + [f'{results[s][m][op]:,.1f}' for m in modes] for op in ops for s in sizes])
comparison = table(['Operation', 'Python source, tree on (µs/op)', 'Native C++, tree on (µs/op)',
                    'Tree-on speed-up', 'Lazy-delete speed-up'],
                   [[op, f'{old["on"][op]:,.1f}', f'{current["on"][op]:,.1f}',
                     f'{old["on"][op] / current["on"][op]:.2f}×',
                     f'{old["lazy"][op] / current["lazy"][op]:.2f}×'] for op in ops])
storage = table(['Rows', 'siql (MB)', 'SQLite (MB)', 'SQLite, indexed (MB)'],
                [[f'{int(s):,}'] + [f'{data["storage"][s][m] / 1e6:.2f}' for m in ['on', 'sqlite', 'sqlite_index']]
                 for s in sizes])
pending_table = table(['Operation', 'Tree off (µs/op)', 'Tree on (µs/op)', 'Tree on + lazy delete (µs/op)',
                       'Lazy vs tree on'],
                      [[op] + [f'{pending[m][op]:,.1f}' for m in ['off', 'on', 'lazy']] +
                       [f'{pending["on"][op] / pending["lazy"][op]:.2f}×']
                       for op in pending['on']])
ratios = [results[s]['off'][op] / results[s]['on'][op]
          for s in sizes for op in ['Read (str)', 'Read (int)', 'Update']]
storage_ratio = data['storage'][big]['on'] / data['storage'][big]['sqlite']
speedups = [old['on'][op] / current['on'][op] for op in ops]
compile_conclusion = ('All five tree-on operations were slower in this run.' if max(speedups) < 1
                      else 'Compilation did not improve every operation in this run.' if min(speedups) < 1
                      else 'All five tree-on operations were faster in this run.')
def measured_date(meta):
    return str(datetime.fromisoformat(meta['measured_at']).astimezone(ZoneInfo('America/Chicago')).date())


date = measured_date(meta)

section = f'''#### Performance: tree off vs on vs lazy delete, and SQLite
This is a dated snapshot; see the separate [CRUD methods review](#crud-performance-review)
for the before/after experiment and its implementation changes.

Each operation was timed on tables of 1,000, 10,000 and 50,000 rows (`name` = random 8-letter string,
`n` = row number), with 300 operations of each kind, taking the median of 3 runs. SiQL ran with all
implementation modules compiled to C++ and the C search engine enabled, with result caching disabled.
Lazy add and both optional composite indexes were off. SQLite ({meta['sqlite']}, through
Python's `sqlite3`) ran the same steps with every statement saved on its own (autocommit) and
`synchronous=OFF`. "SQLite, indexed" also has an index on each column.

| Operation | What's timed in SiQL | What's timed in SQLite |
|---|---|---|
| Create | `table.add(...)`, one row at a time, each saved to the file | `INSERT`, one row per statement |
| Read (str) / Read (int) | `table.find("name", value)` / `table.find("n", value)` | `SELECT rowid ... WHERE name = ?` / `n = ?` |
| Update | `table.editByName("name", old, new)` | `UPDATE ... SET name = ? WHERE name = ?` |
| Delete | `table.find(...)` then `DropRow` for the match | `DELETE ... WHERE name = ?` |

![Time per operation at 50,000 rows](docs/benchmarks/crud.svg)
![Delete time by mode](docs/benchmarks/delete.svg)
![Speed-up with the tree on](docs/benchmarks/speedup.svg)
![Lookup time as the table grows](docs/benchmarks/read_scaling.svg)

{crud}

These measurements show:

- Reads and updates are {min(ratios):.1f}–{max(ratios):.0f}× faster with the tree than without across the tested sizes.
- At 50,000 rows, tree-on integer lookups take {current['on']['Read (int)']:.1f} µs versus
  {current['sqlite_index']['Read (int)']:.1f} µs for indexed SQLite; updates take
  {current['on']['Update']:.1f} versus {current['sqlite_index']['Update']:.1f} µs.
- String lookups include building the row-position map on their first lookup. Integer lookups run next and
  reuse it. These are workload averages, not measurements of warmed-up lookups alone.
- At 50,000 rows, lazy deletes take {current['lazy']['Delete']:.1f} µs versus
  {current['on']['Delete']:,.1f} µs with the plain tree, a {current['on']['Delete'] / current['lazy']['Delete']:.1f}×
  ratio for this snapshot. See the CRUD review for the change that lets eager deletes retain
  bounded position offsets too.

#### Native C++ compared with the same Python source
The current maintained source was run as interpreted Python and as compiled C++ on the same
machine, using the same Python version, data, C search engine, and benchmark settings. The runs were
sequential. The Python run uses an isolated source copy with the identical native C search extension.
Speed-up is Python time divided by native time: above 1× is faster; below 1× is slower.

![Current Python source and native C++ timings at 50,000 rows](docs/benchmarks/native_comparison.svg)
![Speed-up from compiling the library to C++](docs/benchmarks/native_speedup.svg)

{comparison}

Tree-on speed-ups at 50,000 rows range from {min(speedups):.2f}× to {max(speedups):.2f}× across these operations.
{compile_conclusion} File writes, Python object comparisons, and
table-file parsing still contribute to the timings. Sequential runs and timing noise can affect
comparisons. These results describe this workload and machine.

#### Storage
![File size after the creates](docs/benchmarks/storage.svg)

{storage}

At 50,000 rows, SiQL's file is {storage_ratio:.1f}× the size of unindexed SQLite's. Its readable text repeats
column names on every row; SQLite stores rows in binary. SiQL's search tree lives in memory and is rebuilt
when a table loads, while SQLite keeps its indexes in the file. SiQL keeps the whole table in memory;
SQLite reads from disk as needed. Memory use was not measured.

#### Reads and writes while deletes are waiting
The same 50,000-row table, but each operation is timed right after 1,000 random rows were deleted (untimed),
so with lazy delete those deletes are still waiting in the tree. "Read (str), again" repeats the previous
lookups, and "Delete + read" alternates deleting a random row with looking one up. Each phase starts with
a fresh batch of deletes, apart from the repeated lookups.

![With 1,000 deletes waiting](docs/benchmarks/pending.svg)

{pending_table}

The first lookups after lazy deletes clean the branches they read. Repeated string lookups take
{pending['lazy']['Read (str), again']:.1f} µs with lazy delete and {pending['on']['Read (str), again']:.1f} µs
with the plain tree. Mixed deleting and reading takes {pending['lazy']['Delete + read']:.1f} µs with lazy
delete, {pending['on']['Delete + read']:,.1f} µs with the plain tree, and
{pending['off']['Delete + read']:,.1f} µs without a tree. Lazy delete is
{pending['on']['Delete + read'] / pending['lazy']['Delete + read']:.1f}× faster than the plain tree for that workload.

Measured on {date} with SiQL {meta['siql']} (native C++ modules and C search), SQLite {meta['sqlite']},
Python {meta['python']}, Linux (WSL2), Intel Core Ultra 7 165U. Raw native numbers are in
[results.json](docs/benchmarks/results.json); the same-machine Python baseline is in
[results-python.json](docs/benchmarks/results-python.json). Run `python benchmarks/crud.py` for the full
benchmark (`--quick` for a short run, `--replot` to redraw saved results).
Run `python benchmarks/compare_native.py` to redraw the native comparison graphs from both result files.

#### C search functions
The skip lists, whole-column scans (`=`, `!=`, `<`, `<=`, `>`, `>=`, `IS [NOT] NULL`, `LIKE`) and value
extraction for `ORDER BY` use the C engine in `siql._speedups`. The complete distribution also compiles
the table, persistence, parser, interpreter, and server modules to C++ using Cython.
`siql.helpers.search.SPEEDUPS` reports whether the C search engine is active. `SIQL_PURE_PYTHON=1`
selects the reference search algorithms, which are also compiled in the native distribution. The existing
tests verify both search implementations give the same answers.

The interpreter combines `WHERE` scans and tree lookups as sets of row numbers. `ORDER BY` sorts row
numbers and builds only the returned rows. Query-specific timings were not rerun for this CRUD comparison;
the tables above report the operations listed in the benchmark methodology.

'''
readme = root / 'README.md'
text = readme.read_text(encoding='utf-8')
start = text.index('#### Performance:')
end = text.index('\n## ', text.index('#### C search functions', start)) + 1
text = text[:start] + section + text[end:]


def load(name):
    return json.loads((root / 'docs/benchmarks' / name).read_text(encoding='utf-8'))


def replace_tail(heading, marker, replacement):
    global text
    begin = text.index(marker, text.index(heading))
    end = text.index('#### ', begin + 1)
    text = text[:begin] + replacement.rstrip() + '\n\n' + text[end:]


lazy = load('lazy_add_results.json')
lr = lazy['results']
lazy_table = table(['Phase', 'Eager add (ms)', 'Lazy add (ms)'],
                   [[phase] + [f'{lr[mode]["median_ms"][phase]:.2f}' for mode in ['Eager add', 'Lazy add']]
                    for phase in ['Append', 'First read', 'Flush', 'Total']])
replace_tail('#### Lazy additions', 'To bound the queue,', f'''To bound the queue, a column indexes all its pending additions when their count exceeds the larger
of 64 and its currently indexed live-cell count. `purge()` and disabling lazy add finish remaining
work explicitly. Reload and schema/tree rebuilds build ordinary complete indexes. This shifts
indexing cost from insertion to later searches or flushing; it does not defer saving or guarantee
lower total work.

A {lazy['meta']['additions']:,}-row bulk append into a {lazy['meta']['rows']:,}-row table measured:

{lazy_table}

![Eager versus lazy append and deferred work](docs/benchmarks/lazy_add.svg)

Measured on {measured_date(lazy['meta'])} on Windows, Python {lazy['meta']['python']},
{lazy['meta']['repeats']} repeats, with caching disabled and a fresh table each repeat. Append includes
serialization and saving; the first read includes building the position map. Total includes append,
first read, and flushing the remaining index work. The phases show where costs move rather than
claiming a general speed-up. [Raw results](docs/benchmarks/lazy_add_results.json) include each run.
Reproduce with `python benchmarks/lazy_add.py`; use `--replot` to redraw the graph.''')

prefix = load('compressed_search_results.json')
pr = prefix['results']['100000']['workloads']
prefix_table = table(['Exact predicates', 'Tree + narrowed scan (µs/query)', 'Groups, first pass', 'Groups, warm trees'],
                     [[label] + [f'{value:,.2f}' for value in [result['baseline']['latency_us'],
                       result['compressed_first_pass_us'], result['compressed_warm_us']]]
                      for label, result in pr.items()])
replace_tail('#### Compressed column', 'This mode defaults to off.', f'''This mode defaults to off. At 100,000 rows on native Windows (lower is better):

{prefix_table}

The single-column grouped numbers deliberately force the prototype path; public single-column
searches use the ordinary tree. Warm all-column searches took
{pr['All three columns']['compressed_warm_us']:.2f} µs versus
{pr['All three columns']['baseline']['latency_us']:.2f} µs for the ordinary tree, with about
{pr['All three columns']['candidate_rows_mean']:.0f} candidate rows. Single-column grouping visited
about {pr['One column']['candidate_groups_mean']:.0f} groups per query.

Group trees are built lazily; first-pass timings include that cost. Workloads run sequentially,
so later first passes may reuse trees from earlier workloads. Initial group formation is reported
separately. Index storage lies outside the rolling cache budget; cached group results share it.

![Prefix grouping versus the existing search tree](docs/benchmarks/compressed_search.svg)

Measured on {measured_date(prefix['meta'])} with Python {prefix['meta']['python']} on Windows,
{prefix['meta']['queries']} queries per workload, {prefix['meta']['repeats']} repeats, and result caching disabled.
Every query agreed with ordinary search. The fixture has 512 names, ages 0–99, and two-byte values.
[Raw measurements](docs/benchmarks/compressed_search_results.json) include all sizes and repeats.
Reproduce with `python benchmarks/compressed_search.py`; `--replot` redraws the graph.''')

integer = load('integer_search_results.json')
ir = integer['results']['100000']
iw = ir['workloads']
integer_table = table(['Searched columns', 'Ordinary tree (µs/query)', 'Prefix groups',
                        'Integer, low estimate', 'Integer, lower bound'],
                      [[label] + [f'{iw[mode][label]["warm_us"]:,.2f}' for mode in iw]
                       for label in iw['Tree']])
replace_tail('#### Full-row integer', 'At 100,000 rows on native Windows', f'''At 100,000 rows on native Windows, warm exact searches measured:

{integer_table}

The leading pair scanned about {iw['Integer under']['Leading pair']['mean_range_groups']:.0f} integer keys;
the pair omitting the first column scanned about
{iw['Integer under']['Nonleading pair']['mean_range_groups']:,.0f}. The two seeks cover identical ranges:
estimating the initial jump does not remove the need to check earlier matches. Individual timings
vary, so small differences are not evidence of a general advantage for either jump strategy.
Integer index formation and sorting took {ir['formation_ms']['Integer under'] / 1000:.2f} seconds,
excluded from query timing. Mutations and wider fields can make rebuilding costly.

![Full-row integer search comparison](docs/benchmarks/integer_search.svg)

Measured on {measured_date(integer['meta'])}, Python {integer['meta']['python']} on Windows,
{integer['meta']['queries']} queries per workload, {integer['meta']['repeats']} repeats, with caching disabled.
Every result agreed with ordinary search. Modes run sequentially; lower-bound trials reuse the
integer index. Both integer trials use identical direct calls and position conversion.
[Raw results](docs/benchmarks/integer_search_results.json) include smaller sizes and each run.
Reproduce with `python benchmarks/integer_search.py`; `--replot` redraws the graph.''')

traversal = load('traversal_cache_results.json')
tr = traversal['results']['100000']
traversal_table = table(['Workload', 'Results only (µs/query)', 'Inspected nodes (µs/query)', 'Inspected-node hit rate'],
                        [[label, f'{result["modes"]["Results only"]["latency_us"]:.2f}',
                          f'{result["modes"]["All inspected nodes"]["latency_us"]:.2f}',
                          f'{result["modes"]["All inspected nodes"]["hit_rate"]:.1%}']
                         for label, result in tr.items()])
random_nodes = tr['Random distinct']['modes']['All inspected nodes']
random_results = tr['Random distinct']['modes']['Results only']
replace_tail('#### Caching values encountered', 'This mode is off by default', f'''This mode is off by default. At 100,000 rows on native Windows, starting with an empty cache:

{traversal_table}

Random distinct searches retained {random_nodes['retained_bytes'] / 1e6:.2f} MB and
{random_nodes['entries']:,.0f} entries with traversal admissions, versus
{random_results['retained_bytes'] / 1e6:.2f} MB and {random_results['entries']:,.0f} entries for results only.
The separate mutation stress passed {traversal['stress']['query_comparisons']:,} SQLite comparisons
with zero mismatches. `cache_traversed_nodes=False` stops new traversal admissions;
`clear_cache()` removes already admitted entries when comparing modes.

![Result-only versus inspected-node caching](docs/benchmarks/traversal_cache.svg)

Measured on {measured_date(traversal['meta'])}, Python {traversal['meta']['python']} on Windows,
{traversal['meta']['repeats']} repeats using the same live tree. Queries include admission and conversion;
repeated-name timing includes the initial cache fill.
[Raw results](docs/benchmarks/traversal_cache_results.json) include all runs and stress results.
Reproduce with `python benchmarks/traversal_cache.py`; `--replot` redraws the graph.''')

batch = load('batch_results.json')
br = batch['results']['50000']['workloads']
batch_table = table(['Workload', 'Independent (µs/query)', 'Tree order + chain (µs/query)', 'Speed-up'],
                    [[label, f'{result["latency_us"]["Independent"]:.2f}',
                      f'{result["latency_us"]["Tree order: chain"]:.2f}',
                      f'{result["latency_us"]["Independent"] / result["latency_us"]["Tree order: chain"]:.2f}×']
                     for label, result in br.items()])
replace_tail('#### Chained batch', 'At 50,000 rows with 300-query batches:', f'''At 50,000 rows with 300-query batches:

{batch_table}

All batch modes matched independent exact searches. Batching saves calls and repeated branch work;
the fresh-path comparison isolates additional traversal effects.
Measured on {measured_date(batch['meta'])}, native Python {batch['meta']['python']} on WSL2,
{batch['meta']['repeats']} repeats with {batch['meta']['batches_per_pass']} warm batches per pass, caching disabled.
Nearby queries are shuffled contiguous numbers within one branch; repeats sample eight names.
Timing includes ordering and restoring input order. [Raw results](docs/benchmarks/batch_results.json)
include batch sizes 8, 32, 128, and 512. Reproduce with `python benchmarks/batch.py`;
`--replot` redraws the graphs.''')

win = load('windows_cache_tests/cache_sqlite_results.json')
ws = load('windows_cache_tests/cache_stress_results.json')
lin = load('cache_sqlite_results.json')
ls = load('cache_stress_results.json')
cw = win['results']['100000']['workloads']
cache_table = table(['Workload', 'SiQL tree, cache off', 'SiQL cache', 'SQLite in memory', 'SQLite file, warm'],
                    [[label] + [f'{value:.2f}' for value in result['latency_us'].values()] for label, result in cw.items()])
snapshots = {snapshot['stage']: snapshot for snapshot in ws['memory']['snapshots']}
fill = snapshots['fill 100 MB budget']
shrink = snapshots['shrink to 1 MB']
lrp = lin['results']['100000']['workloads']['Repeated names']['latency_us']
replace_tail('#### Cache speed and stress', 'The cache and tree were compared', f'''The cache and tree were compared with indexed SQLite on 1,000, 10,000, 50,000, and 100,000 rows,
up to {win['meta']['queries']:,} queries per workload and {win['meta']['repeats']} repeats. Timings include
returning all row IDs. Repeated names use 32 names; fan-out uses 16 categories. Distinct-key traces
start with an empty result cache. SQLite statements and pages are warm; plans use covering indexes.
Fixtures and prewarming are excluded.

Native Windows results at 100,000 rows (µs/query; lower is better):

{cache_table}

The file-backed case includes normal file and locking work, with warm pages. It does not measure
cold-disk reads or durable writes. SiQL keeps rows resident; these compare indexed lookups rather
than general SQL performance or total database memory.

![Cached repeat lookup against SQLite on Windows](docs/benchmarks/windows_cache_tests/cache_sqlite_hot.svg)
![Distinct-key lookup against SQLite on Windows](docs/benchmarks/windows_cache_tests/cache_sqlite_cold.svg)
![Rolling cache under memory pressure on Windows](docs/benchmarks/windows_cache_tests/cache_stress_memory.svg)

![Cached repeat lookup against SQLite on WSL](docs/benchmarks/cache_sqlite_hot.svg)
![Distinct-key lookup against SQLite on WSL](docs/benchmarks/cache_sqlite_cold.svg)
![Rolling cache under memory pressure on WSL](docs/benchmarks/cache_stress_memory.svg)

Stress passed on both Windows and WSL: {ws['mutations']['steps']:,} mixed operations per platform,
{ws['mutations']['query_comparisons']:,} SQLite comparisons each, zero mismatches, and matching compacted reloads.
The trace includes edits, appends, lazy deletes, runtime resizes, and column renames. Each platform
also ran {ws['memory']['queries']:,} cache-pressure queries. Windows filled to
{fill['cache_info']['bytes'] / 1e6:.2f} MB of accounted cache with
{fill['traced_growth_bytes'] / 1e6:.2f} MB of traced growth. Shrinking to 1 MB left
{shrink['traced_growth_bytes'] / 1e6:.2f} MB traced; disabling removed all entries.
Traced allocation growth excludes existing rows and is not process RSS. Tracing affects stress timing.

Measured on {measured_date(win['meta'])} (Windows) and {measured_date(lin['meta'])} (WSL2),
Intel Core Ultra 7 165U. Windows: Python {win['meta']['python']} / SQLite {win['meta']['sqlite']};
WSL: Python {lin['meta']['python']} / SQLite {lin['meta']['sqlite']}. WSL repeated-name results were
{lrp['SiQL cache']:.2f} µs for SiQL cache, {lrp['SQLite memory']:.2f} µs for SQLite in memory, and
{lrp['SQLite file, warm']:.2f} µs for warm file SQLite.
Raw [Windows lookups](docs/benchmarks/windows_cache_tests/cache_sqlite_results.json),
[Windows stress](docs/benchmarks/windows_cache_tests/cache_stress_results.json),
[WSL lookups](docs/benchmarks/cache_sqlite_results.json), and
[WSL stress](docs/benchmarks/cache_stress_results.json) include runs and snapshots.
The separate CRUD and batch comparisons below use caching disabled.
Reproduce with `python benchmarks/cache_sqlite.py`; `--no-plots` supports environments without
Matplotlib, `--output DIRECTORY` separates platform results, and `--replot` redraws the graphs.''')

readme.write_text(text, encoding='utf-8', newline='\n')
