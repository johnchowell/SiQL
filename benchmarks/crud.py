"""CRUD timings for siql tables with the search tree on and off, compared with SQLite.

Run from the repository root (needs matplotlib: pip install -e . --group bench):
    python benchmarks/crud.py [--quick]     # time everything, save results.json and charts
    python benchmarks/crud.py --replot      # redraw the charts from the saved results.json
Writes docs/benchmarks/results.json and SVG charts, and prints Markdown tables of the results.
"""
import json
import os
import platform
import random
import sqlite3
import statistics
import string
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.machinery import ExtensionFileLoader
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    sys.exit("The benchmark needs matplotlib: pip install -e . --group bench")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from siql import Table, TableDiff, __version__  # noqa: E402
from siql.helpers.search import SPEEDUPS  # noqa: E402
from siql.models import AddRow, DropRow  # noqa: E402

OUT = ROOT / "docs" / "benchmarks"
OPS = ["Create", "Read (str)", "Read (int)", "Update", "Delete"]
PENDING_OPS = ["Read (str)", "Read (str), again", "Read (int)", "Update", "Append", "Delete + read"]
# mode -> (label, options): siql Table keyword arguments, or {"sqlite": ...} for SQLite
MODES = {
    "off": ("tree off", {"tree": False}),
    "on": ("tree on", {"tree": True}),
    "lazy": ("tree on + lazy delete", {"tree": True, "lazy_delete": True}),
    "sqlite": ("SQLite", {"sqlite": "plain"}),
    "sqlite_index": ("SQLite, indexed", {"sqlite": "indexed"}),
}
SIQL_MODES = ["off", "on", "lazy"]


def run(size: int, options: dict, lookups: int, seed: int = 1) -> tuple[dict[str, float], int]:
    """Microseconds per operation for each CRUD step on a table of `size` rows, and the file size after the creates."""
    if "sqlite" in options:
        return run_sqlite(size, options["sqlite"] == "indexed", lookups, seed)
    rng = random.Random(seed)
    names = ["".join(rng.choices(string.ascii_letters, k=8)) for _ in range(size)]
    probes = rng.sample(range(size), lookups)
    t = Table(file="t.siql", **options)
    t.addCols(name=str, n=int)
    us = {}

    start = time.perf_counter()
    for i, name in enumerate(names):
        t.add(name=name, n=i)
    us["Create"] = (time.perf_counter() - start) / size * 1e6
    created = os.path.getsize("t.siql")

    start = time.perf_counter()
    for i in probes:
        t.find("name", names[i])
    us["Read (str)"] = (time.perf_counter() - start) / lookups * 1e6

    start = time.perf_counter()
    for i in probes:
        t.find("n", i)
    us["Read (int)"] = (time.perf_counter() - start) / lookups * 1e6

    start = time.perf_counter()
    for i in probes:
        t.editByName("name", names[i], names[i] + "!")
    us["Update"] = (time.perf_counter() - start) / lookups * 1e6

    start = time.perf_counter()
    for i in probes:
        for j in reversed(t.find("name", names[i] + "!")):
            TableDiff([DropRow(j)]).apply(t)
    us["Delete"] = (time.perf_counter() - start) / lookups * 1e6

    assert len(t.rows) == size - lookups
    return us, created


def run_sqlite(size: int, indexed: bool, lookups: int, seed: int = 1) -> tuple[dict[str, float], int]:
    """The same steps on SQLite, each statement saved on its own like siql's changes."""
    rng = random.Random(seed)
    names = ["".join(rng.choices(string.ascii_letters, k=8)) for _ in range(size)]
    probes = rng.sample(range(size), lookups)
    db = sqlite3.connect("t.db", isolation_level=None)  # autocommit: every statement is its own saved change
    db.execute("PRAGMA synchronous=OFF")  # like siql: survives a crashed process, not a power cut
    db.execute("CREATE TABLE t (name TEXT, n INTEGER)")
    if indexed:
        db.execute("CREATE INDEX t_name ON t(name)")
        db.execute("CREATE INDEX t_n ON t(n)")
    us = {}

    def timed(label, statements, count):
        start = time.perf_counter()
        for sql, params in statements:
            db.execute(sql, params).fetchall()
        us[label] = (time.perf_counter() - start) / count * 1e6

    timed("Create", (("INSERT INTO t VALUES (?, ?)", (x, i)) for i, x in enumerate(names)), size)
    created = os.path.getsize("t.db")
    timed("Read (str)", (("SELECT rowid FROM t WHERE name = ?", (names[i],)) for i in probes), lookups)
    timed("Read (int)", (("SELECT rowid FROM t WHERE n = ?", (i,)) for i in probes), lookups)
    timed("Update", (("UPDATE t SET name = ? WHERE name = ?", (names[i] + "!", names[i])) for i in probes), lookups)
    timed("Delete", (("DELETE FROM t WHERE name = ?", (names[i] + "!",)) for i in probes), lookups)
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == size - lookups
    db.close()
    return us, created


def run_pending(size: int, options: dict, lookups: int, deletes: int, seed: int = 1) -> dict[str, float]:
    """Microseconds per operation right after `deletes` random rows were deleted. Each phase deletes a fresh
    batch first (untimed), except "again", which repeats the previous lookups."""
    rng = random.Random(seed)
    names = ["".join(rng.choices(string.ascii_letters, k=8)) for _ in range(size)]
    t = Table(**options)
    t.addCols(name=str, n=int)
    TableDiff([AddRow(i, {"name": x, "n": i}) for i, x in enumerate(names)]).apply(t)
    t.find("n", 0)  # build the position map, as any earlier lookup would have

    def delete_batch():
        for _ in range(deletes):
            TableDiff([DropRow(rng.randrange(len(t.rows)))]).apply(t)

    def sample(column):
        return [r.col(column).value for r in rng.sample(t.rows, lookups)]

    def timed(op) -> float:
        start = time.perf_counter()
        for i in range(lookups):
            op(i)
        return (time.perf_counter() - start) / lookups * 1e6

    def delete_and_read(i):
        TableDiff([DropRow(rng.randrange(len(t.rows)))]).apply(t)
        t.find("name", t.rows[rng.randrange(len(t.rows))].col("name").value)

    us = {}
    delete_batch()
    probes = sample("name")
    us["Read (str)"] = timed(lambda i: t.find("name", probes[i]))
    us["Read (str), again"] = timed(lambda i: t.find("name", probes[i]))
    delete_batch()
    ints = sample("n")
    us["Read (int)"] = timed(lambda i: t.find("n", ints[i]))
    delete_batch()
    olds = sample("name")
    us["Update"] = timed(lambda i: t.editByName("name", olds[i], olds[i] + "!"))
    delete_batch()
    us["Append"] = timed(lambda i: t.add(name=f"new{i}", n=-i))
    delete_batch()
    us["Delete + read"] = timed(delete_and_read)
    return us


# --- Charts -----------------------------------------------------------------------------------------------------

COLORS = ["#9aa5b1", "#2b7bb9", "#e07b39", "#5b8c5a", "#8e6bbf"]
plt.rcParams.update({
    "svg.fonttype": "none",  # keep text as text so the SVGs stay small and searchable
    "font.family": "sans-serif",
    "font.size": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def _fmt(v: float, unit: str) -> str:
    if unit == "ratio":
        return f"{v:.2f}×"
    if unit == "x":
        return f"{v:.1f}×"
    if unit == "MB":
        return f"{v:.2f}"
    return f"{v:,.0f}" if v >= 100 else f"{v:.1f}" if v >= 10 else f"{v:.2f}"


def _save(fig, ax, path: Path, title: str, y_label: str):
    ax.set_title(title, fontweight="bold", pad=28)
    ax.set_ylabel(y_label)
    ax.grid(axis="y", color="#e3e6ea")
    ax.set_axisbelow(True)
    handles = ax.get_legend_handles_labels()[0]
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncols=min(len(handles), 5), fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    # Matplotlib puts trailing spaces in SVG paths; keep generated artifacts clean in Git.
    path.write_text("\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()) + "\n",
                    encoding="utf-8", newline="\n")
    plt.close(fig)


def bar_chart(path: Path, title: str, categories: list[str], series: list[tuple[str, list[float]]],
              y_label: str, unit: str = "us", log: bool = False, ref: float | None = None,
              colors: list[str] | None = None):
    fig, ax = plt.subplots(figsize=(10, 4.5) if len(series) > 3 else (8, 4))
    width = 0.8 / len(series)
    for s, (label, values) in enumerate(series):
        xs = [c + (s - (len(series) - 1) / 2) * width for c in range(len(categories))]
        bars = ax.bar(xs, values, width * 0.95, label=label, color=(colors or COLORS)[s])
        ax.bar_label(bars, [_fmt(v, unit) for v in values], fontsize=7 if len(series) > 3 else 8, padding=2)
    ax.set_xticks(range(len(categories)), categories)
    if log:
        ax.set_yscale("log")
    if ref is not None:
        ax.axhline(ref, color="#c0392b", linestyle="--", linewidth=1, label="no change")
    ax.margins(y=0.15)
    _save(fig, ax, path, title, y_label)


def scaling_chart(path: Path, title: str, sizes: list[int], series: list[tuple[str, str, str, list[float]]],
                  y_label: str):
    fig, ax = plt.subplots(figsize=(8, 4))
    for label, style, color, values in series:
        ax.plot(sizes, values, style, label=label, color=color, linewidth=2, markersize=6)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks(sizes, [f"{s:,}" for s in sizes])
    ax.minorticks_off()
    ax.set_xlabel("rows in table")
    _save(fig, ax, path, title, y_label)


# --- Main -------------------------------------------------------------------------------------------------------

def _timed_modes(fn, repeats: int, ops: list[str], modes: list[str]) -> tuple[dict[str, dict[str, float]], dict]:
    """Median microseconds per op for each mode, running `fn(options)` in a fresh folder each time, and whatever
    else `fn` returned on its first run for each mode."""
    runs, extra = {mode: [] for mode in modes}, {}
    cwd = os.getcwd()
    for _ in range(repeats):
        for mode in modes:  # alternate so background noise affects every mode alike
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)  # siql tables and SQLite databases are written to the current folder
                try:
                    us, other = fn(MODES[mode][1])
                finally:
                    os.chdir(cwd)
            runs[mode].append(us)
            extra.setdefault(mode, other)
    return {mode: {op: statistics.median(r[op] for r in rs) for op in ops} for mode, rs in runs.items()}, extra


def main():
    quick = "--quick" in sys.argv
    if "--replot" in sys.argv:
        saved = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        charts({int(size): r for size, r in saved["results"].items()}, saved.get("pending"),
               {int(size): r for size, r in saved.get("storage", {}).items()})
        return
    sizes = [1_000, 5_000] if quick else [1_000, 10_000, 50_000]
    lookups = 50 if quick else 300
    repeats = 1 if quick else 3
    results, storage = {}, {}
    for size in sizes:
        results[size], storage[size] = _timed_modes(lambda options: run(size, options, lookups), repeats, OPS,
                                                    list(MODES))
        for mode in MODES:
            print(f"{size:>6} rows, {mode:<12}: "
                  + ", ".join(f"{op} {results[size][mode][op]:,.1f}" for op in OPS) + " us/op", file=sys.stderr)

    deletes = 100 if quick else 1_000
    pending_results, _ = _timed_modes(
        lambda options: (run_pending(sizes[-1], options, lookups, deletes), None), repeats, PENDING_OPS, SIQL_MODES)
    pending = {"rows": sizes[-1], "deletes": deletes, "results": pending_results}
    for mode in SIQL_MODES:
        print(f"pending, {mode:<4}: " + ", ".join(f"{op} {pending['results'][mode][op]:,.1f}" for op in PENDING_OPS)
              + " us/op", file=sys.stderr)

    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"siql": __version__, "c_speedups": SPEEDUPS, "sqlite": sqlite3.sqlite_version,
            "implementation": "native C++ (Cython)" if isinstance(sys.modules["siql.models.table"].__loader__,
                                                                  ExtensionFileLoader) else "Python",
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "sqlite_settings": "autocommit, synchronous=OFF", "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}", "machine": platform.processor() or platform.machine(),
            "lookups": lookups, "repeats": repeats, "unit": "microseconds per op (median of repeats)",
            "storage_unit": "bytes on disk after the creates"}
    (OUT / "results.json").write_text(
        json.dumps({"meta": meta, "results": results, "pending": pending, "storage": storage}, indent=2),
        encoding="utf-8")
    charts(results, pending, storage)

    print("| Operation | Rows | " + " | ".join(f"{MODES[m][0]} (µs/op)" for m in MODES) + " |")
    print("|---|---:|" + "---:|" * len(MODES))
    for op in OPS:
        for s in sizes:
            print(f"| {op} | {s:,} | " + " | ".join(f"{results[s][m][op]:,.1f}" for m in MODES) + " |")
    print("\n| Rows | " + " | ".join(f"{MODES[m][0]} (MB)" for m in ("on", "sqlite", "sqlite_index")) + " |")
    print("|---:|---:|---:|---:|")
    for s in sizes:
        print(f"| {s:,} | " + " | ".join(f"{storage[s][m] / 1e6:.2f}" for m in ("on", "sqlite", "sqlite_index")) + " |")
    print(f"\nWith {deletes:,} deletes waiting, {sizes[-1]:,} rows:\n")
    print("| Operation | Tree off (µs/op) | Tree on (µs/op) | Tree on + lazy delete (µs/op) | Lazy vs tree on |")
    print("|---|---:|---:|---:|---:|")
    for op in PENDING_OPS:
        off, on, lazy = (pending["results"][mode][op] for mode in SIQL_MODES)
        print(f"| {op} | {off:,.1f} | {on:,.1f} | {lazy:,.1f} | {on / lazy:.2f}× |")
    print(f"\n{meta}")


def charts(results: dict[int, dict], pending: dict | None = None, storage: dict[int, dict] | None = None):
    sizes = sorted(results)
    largest = sizes[-1]
    labels = [f"{s:,} rows" for s in sizes]
    modes = [m for m in MODES if m in results[largest]]
    colors = [COLORS[list(MODES).index(m)] for m in modes]
    if pending:
        bar_chart(OUT / "pending.svg",
                  f"With {pending['deletes']:,} deletes waiting, {pending['rows']:,} rows (log scale, lower is better)",
                  PENDING_OPS, [(MODES[mode][0], [pending["results"][mode][op] for op in PENDING_OPS])
                                for mode in SIQL_MODES],
                  "microseconds per operation", log=True)
    if storage:
        storage_modes = [("siql", "on"), ("SQLite", "sqlite"), ("SQLite, indexed", "sqlite_index")]
        bar_chart(OUT / "storage.svg", "File size after the creates (lower is better)", labels,
                  [(label, [storage[s][mode] / 1e6 for s in sizes]) for label, mode in storage_modes],
                  "megabytes", unit="MB", colors=[COLORS[1], COLORS[3], COLORS[4]])
    bar_chart(OUT / "crud.svg", f"Time per operation, {largest:,} rows (log scale, lower is better)", OPS,
              [(MODES[mode][0], [results[largest][mode][op] for op in OPS]) for mode in modes],
              "microseconds per operation", log=True, colors=colors)
    bar_chart(OUT / "speedup.svg", "Speed-up with the tree on (tree off time ÷ tree on time, higher is better)", OPS,
              [(label, [results[s]["off"][op] / results[s]["on"][op] for op in OPS]) for s, label in zip(sizes, labels)],
              "times faster", unit="x", ref=1.0)
    bar_chart(OUT / "delete.svg", "Delete time by mode (log scale, lower is better)", labels,
              [(MODES[mode][0], [results[s][mode]["Delete"] for s in sizes]) for mode in modes],
              "microseconds per delete", log=True, colors=colors)
    scaling_chart(
        OUT / "read_scaling.svg", "Read (str) time as the table grows (log scales, lower is better)", sizes,
        [(MODES[mode][0], "-" + marker, COLORS[list(MODES).index(mode)], [results[s][mode]["Read (str)"] for s in sizes])
         for mode, marker in (("off", "o"), ("on", "s"), ("sqlite", "^"), ("sqlite_index", "D")) if mode in modes],
        "microseconds per lookup")


if __name__ == "__main__":
    main()
