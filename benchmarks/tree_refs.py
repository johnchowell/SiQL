"""One fresh-process sample for the resident tree reference comparison.

Set SIQL_DIRECT_REFS=0 or 1 before starting Python. The result cache is disabled;
each mode uses 30,000 rows with unique names and 256 duplicate group values.
"""
import gc
import json
import os
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from siql import Table, TableDiff  # noqa: E402
from siql.models import AddRow  # noqa: E402

SIZE = 30_000
QUERIES = 3_000
names = [f"item{i:06}" for i in range(SIZE)]
groups = [f"group{i % 256:03}" for i in range(SIZE)]

tracemalloc.start()
table = Table(cache_max_bytes=0)
table.addCols(name=str, group=str)
started = time.perf_counter_ns()
TableDiff([AddRow(i, {"name": names[i], "group": groups[i]}) for i in range(SIZE)]).apply(table)
build_ms = (time.perf_counter_ns() - started) / 1e6
gc.collect()
memory_bytes = tracemalloc.get_traced_memory()[0]
tracemalloc.stop()

unique = [names[(i * 7919) % SIZE] for i in range(QUERIES)]
fanout = [groups[(i * 137) % SIZE] for i in range(QUERIES)]


def timed(action):
    samples = []
    for _ in range(5):
        gc.disable()
        try:
            started = time.perf_counter_ns()
            action()
            samples.append((time.perf_counter_ns() - started) / QUERIES / 1000)
        finally:
            gc.enable()
    return round(statistics.median(samples), 3)


unique_us = timed(lambda: [table.find("name", value) for value in unique])
fanout_us = timed(lambda: [table.find("group", value) for value in fanout])
batch_unique_us = timed(lambda: table.find_batch("name", unique))
batch_fanout_us = timed(lambda: table.find_batch("group", fanout))

# Isolate index maintenance from persistence and row-position bookkeeping.
cells = table.cols["name"][:QUERIES]
index = table.index["name"]
edit_us = timed(lambda: [setattr(cell, "value", cell.value + "x") for cell in cells])
started = time.perf_counter_ns()
for cell in cells:
    index.remove(cell)
index_remove_us = round((time.perf_counter_ns() - started) / QUERIES / 1000, 3)

print(json.dumps({
    "refs": os.environ.get("SIQL_DIRECT_REFS"), "rows": SIZE, "queries": QUERIES,
    "build_ms": round(build_ms, 1), "memory_bytes": memory_bytes,
    "unique_us": unique_us, "fanout_us": fanout_us,
    "batch_unique_us": batch_unique_us, "batch_fanout_us": batch_fanout_us,
    "edit_us": edit_us, "index_remove_us": index_remove_us,
}))
