# CRUD methods review

The October 16, 2026 changes remove repeated work from the existing engine. Public
features, defaults, row ordering, immediate commit writes, and file format stay the
same. The Python and C# implementations share the deletion optimization; runtime
specific changes bring their common comparisons and serialization closer in cost.

## Findings and changes

| Method | Assessment | Change |
|---|---|---|
| Row-to-position map | Eager deletion discarded the map. The next successful lookup rebuilt a dictionary for every surviving row, making repeated delete/lookups scale poorly. | Both runtimes now retain sorted deletion offsets for eager as well as lazy removal. Offsets are bounded by roughly a quarter of live rows plus 64; exceeding that bound or inserting in the middle invalidates the map. Tree cleanup still follows the existing eager/lazy setting. |
| Python commit serialization | Every change was converted to text and immediately parsed into an AST before being applied. Bulk commits constructed a second representation of all their values. | Exact built-in literal types use a bounded recursive check. Custom representations, subclasses, nonfinite numbers, cycles, and deep containers keep the original parser fallback and its acceptance/error behavior. The text representation is unchanged. |
| C# numeric comparisons | Even two ordinary integers took the general rational-number path; integer conversion used decimal text. | Matching `int`, `long`, and `double` types compare directly. Integer-to-BigInteger conversion avoids text. Mixed numeric types retain exact rational comparisons, including integer/float boundaries beyond 2^53. |
| C# string comparisons | Every comparison allocated two Unicode iterators. This work repeats throughout skip-list traversal. | A direct scalar-value loop preserves Python-compatible Unicode ordering without those allocations. UTF-16 ordinal ordering would be incorrect for supplementary characters. |
| C# single-key skip-list reads | Ordinary lookups allocated a predecessor array and an empty trace dictionary, although neither was used. | Direct traversal returns the same matching collection. Insertion/removal, traced lookups, and chained batches retain their required bookkeeping. |

The Python profile of a 2,000-row CRUD/reopen/bulk-insert run recorded 4,804 calls
to `ast.literal_eval`, consuming 137 ms of 1.306 seconds. That includes replay,
which still parses stored data. The compiled implementation's internal time is
partly attributed to its Python caller, so this is not a detailed native CPU profile.

## Where the current methods still cost time

- **Individual writes:** Every committed change opens, appends to, and closes the
  table file. Keeping immediate visibility and current file ownership semantics
  means this review does not buffer several user commits together. Existing SQL
  multi-row INSERT and `TableDiff` already support batching when the application
  wants one atomic change. Bulk and individual insert timings therefore represent
  different commit boundaries.
- **Positional row storage:** Removing an element still shifts the row list and
  each column list. The new offsets avoid rebuilding a second dictionary; they
  do not turn arbitrary deletion into constant time. Stable internal row IDs or
  tombstoned storage would need a broader design to preserve the public row lists
  and current integer positions.
- **All-column indexing:** Every indexed column adds insertion, update, and
  deletion work. First-character/digit branches are coarse and can be skewed;
  the skip lists inside them provide the actual logarithmic search. Compiling
  Python syntax through Cython does not remove Python object allocation and
  comparison costs automatically.
- **Optional composite indexes:** Prefix grouping can help selective multi-column
  queries but costs memory and maintenance. Packing every field into one ordered
  integer is least useful when leading fields are absent: the under-estimated
  jump is a heuristic, and exact completeness still requires scanning in both
  directions. Neither mode should become the default based on a favorable query
  alone. Their existing switches remain available.
- **Caches and deferred work:** Repeated-result caching helps repeated reads;
  column invalidation can erase that benefit during writes. Caching passed nodes
  adds admission/accounting work. Lazy insertion/deletion shifts work into later
  searches and purge, so total write/read/flush cost matters. These options retain
  their defaults. The cache budget covers cached entries, not all table/index RSS.

## Measurements and reproduction

See the [README table](../README.md#crud-performance-review) and
[before/after graph](benchmarks/crud_review/comparison.svg). Raw samples:
[Python before](benchmarks/crud_review/python_before.json),
[Python after](benchmarks/crud_review/python_after.json),
[C# before](benchmarks/crud_review/dotnet_before.json), and
[C# after](benchmarks/crud_review/dotnet_after.json).

Both harnesses use 10,000 and 50,000 rows, names `name00000000` onward, one integer
column, 500 distinct probes per phase, and five runs on fresh temporary tables.
Probes use `(i * 7919) % row_count`. Trees are enabled; caching, composite indexes,
and lazy flags are disabled. A small untimed run warms code paths. Read phases
follow an untimed first lookup that builds the row-position map. Updates change
the searched name; deletion includes lookup. Batch-create timing starts after
constructing the operations and uses one commit. Verification and file reopen
checks run outside the measured phases. Times are wall-clock microseconds per row
or operation, including immediate writes for mutations.

Baseline: commit `46a9269`, before this review. Python is the Windows CPython 3.12
native build; C# is a Release build on .NET 8. Before and after runs are sequential,
not randomized/interleaved trials, and this is a shared development machine.
Short build/test activity overlapped portions of baseline collection. Raw samples
are retained; small differences, particularly individual write times, should not
be treated as general speed guarantees. C# individual updates measured 363.59 µs
before and 396.75 µs after: about 9% slower, with overlapping sample ranges. This
review does not establish an individual-update improvement for C#. This fixture is not a new SQLite comparison,
a memory benchmark, or coverage of every cache/index/workload combination.

```powershell
python setup.py build_ext --inplace
python benchmarks/crud_review.py --output docs/benchmarks/crud_review/python_after.json
dotnet build dotnet/SiQL.Benchmarks -c Release
dotnet dotnet/SiQL.Benchmarks/bin/Release/net8.0/SiQL.Benchmarks.dll docs/benchmarks/crud_review/dotnet_after.json
python benchmarks/plot_crud_review.py  # requires matplotlib
```

To reproduce the baseline, run the same harnesses against the baseline revision's
compiled implementations before applying the changes. Do not label the current
implementation's measurements as a baseline just by changing the output filename.

Validation includes the full Python suite, shared Python/C# fixtures and file
round-trips, plus explicit cases for deletion-offset compaction, eager cleanup,
append, middle insertion, rollback, literal fallback behavior, numeric precision,
NaN, signed zero, and Unicode ordering.
