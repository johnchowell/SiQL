"""CRUD timings for siql tables with the search tree on and off.

Run from the repository root (needs matplotlib: pip install -e . --group bench):
    python benchmarks/crud.py [--quick]     # time everything, save results.json and charts
    python benchmarks/crud.py --replot      # redraw the charts from the saved results.json
Writes docs/benchmarks/results.json and SVG charts, and prints Markdown tables of the results.
"""
import json
import os
import platform
import random
import statistics
import string
import sys
import tempfile
import time
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
from siql.models import AddRow, DropRow  # noqa: E402

OUT = ROOT / "docs" / "benchmarks"
OPS = ["Create", "Read (str)", "Read (int)", "Update", "Delete"]
PENDING_OPS = ["Read (str)", "Read (str), again", "Read (int)", "Update", "Append", "Delete + read"]
# mode -> (label, Table keyword arguments)
MODES = {
    "off": ("tree off", {"tree": False}),
    "on": ("tree on", {"tree": True}),
    "lazy": ("tree on + lazy delete", {"tree": True, "lazy_delete": True}),
}


def run(size: int, options: dict, lookups: int, seed: int = 1) -> dict[str, float]:
    """Microseconds per operation for each CRUD step on a table of `size` rows."""
    rng = random.Random(seed)
    names = ["".join(rng.choices(string.ascii_letters, k=8)) for _ in range(size)]
    probes = rng.sample(range(size), lookups)
    t = Table(**options)
    t.addCols(name=str, n=int)
    us = {}

    start = time.perf_counter()
    for i, name in enumerate(names):
        t.add(name=name, n=i)
    us["Create"] = (time.perf_counter() - start) / size * 1e6

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
    return us


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

COLORS = ["#9aa5b1", "#2b7bb9", "#e07b39"]
plt.rcParams.update({
    "svg.fonttype": "none",  # keep text as text so the SVGs stay small and searchable
    "font.family": "sans-serif",
    "font.size": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def _fmt(v: float, unit: str) -> str:
    if unit == "x":
        return f"{v:.1f}×"
    return f"{v:,.0f}" if v >= 100 else f"{v:.1f}" if v >= 10 else f"{v:.2f}"


def _save(fig, ax, path: Path, title: str, y_label: str):
    ax.set_title(title, fontweight="bold", pad=28)
    ax.set_ylabel(y_label)
    ax.grid(axis="y", color="#e3e6ea")
    ax.set_axisbelow(True)
    handles = ax.get_legend_handles_labels()[0]
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncols=min(len(handles), 4), fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def bar_chart(path: Path, title: str, categories: list[str], series: list[tuple[str, list[float]]],
              y_label: str, unit: str = "us", log: bool = False, ref: float | None = None):
    fig, ax = plt.subplots(figsize=(8, 4))
    width = 0.8 / len(series)
    for s, (label, values) in enumerate(series):
        xs = [c + (s - (len(series) - 1) / 2) * width for c in range(len(categories))]
        bars = ax.bar(xs, values, width * 0.95, label=label, color=COLORS[s])
        ax.bar_label(bars, [_fmt(v, unit) for v in values], fontsize=8, padding=2)
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

def _timed_modes(fn, repeats: int, ops: list[str]) -> dict[str, dict[str, float]]:
    """Median microseconds per op for each mode, running `fn(options)` in a fresh folder each time."""
    runs = {mode: [] for mode in MODES}
    cwd = os.getcwd()
    for _ in range(repeats):
        for mode, (_, options) in MODES.items():  # alternate so background noise affects every mode alike
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)  # every Table writes a .siql file
                try:
                    runs[mode].append(fn(options))
                finally:
                    os.chdir(cwd)
    return {mode: {op: statistics.median(r[op] for r in rs) for op in ops} for mode, rs in runs.items()}


def main():
    quick = "--quick" in sys.argv
    if "--replot" in sys.argv:
        saved = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        charts({int(size): r for size, r in saved["results"].items()}, saved.get("pending"))
        return
    sizes = [1_000, 5_000] if quick else [1_000, 10_000, 50_000]
    lookups = 50 if quick else 300
    repeats = 1 if quick else 3
    results = {}
    for size in sizes:
        results[size] = _timed_modes(lambda options: run(size, options, lookups), repeats, OPS)
        for mode in MODES:
            print(f"{size:>6} rows, {mode:<4}: "
                  + ", ".join(f"{op} {results[size][mode][op]:,.1f}" for op in OPS) + " us/op", file=sys.stderr)

    deletes = 100 if quick else 1_000
    pending = {"rows": sizes[-1], "deletes": deletes, "results": _timed_modes(
        lambda options: run_pending(sizes[-1], options, lookups, deletes), repeats, PENDING_OPS)}
    for mode in MODES:
        print(f"pending, {mode:<4}: " + ", ".join(f"{op} {pending['results'][mode][op]:,.1f}" for op in PENDING_OPS)
              + " us/op", file=sys.stderr)

    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"siql": __version__, "python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
            "machine": platform.processor() or platform.machine(), "lookups": lookups, "repeats": repeats,
            "unit": "microseconds per op (median of repeats)"}
    (OUT / "results.json").write_text(json.dumps({"meta": meta, "results": results, "pending": pending}, indent=2),
                                      encoding="utf-8")
    charts(results, pending)

    print("| Operation | Rows | Tree off (µs/op) | Tree on (µs/op) | Speed-up | Tree on + lazy delete (µs/op) | Speed-up |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for op in OPS:
        for s in sizes:
            off, on, lazy = (results[s][mode][op] for mode in ("off", "on", "lazy"))
            print(f"| {op} | {s:,} | {off:,.1f} | {on:,.1f} | {off / on:.2f}× | {lazy:,.1f} | {off / lazy:.2f}× |")
    print(f"\nWith {deletes:,} deletes waiting, {sizes[-1]:,} rows:\n")
    print("| Operation | Tree off (µs/op) | Tree on (µs/op) | Tree on + lazy delete (µs/op) | Lazy vs tree on |")
    print("|---|---:|---:|---:|---:|")
    for op in PENDING_OPS:
        off, on, lazy = (pending["results"][mode][op] for mode in ("off", "on", "lazy"))
        print(f"| {op} | {off:,.1f} | {on:,.1f} | {lazy:,.1f} | {on / lazy:.2f}× |")
    print(f"\n{meta}")


def charts(results: dict[int, dict], pending: dict | None = None):
    sizes = sorted(results)
    largest = sizes[-1]
    labels = [f"{s:,} rows" for s in sizes]
    if pending:
        bar_chart(OUT / "pending.svg",
                  f"With {pending['deletes']:,} deletes waiting, {pending['rows']:,} rows (log scale, lower is better)",
                  PENDING_OPS, [(label, [pending["results"][mode][op] for op in PENDING_OPS])
                                for mode, (label, _) in MODES.items()],
                  "microseconds per operation", log=True)
    bar_chart(OUT / "crud.svg", f"Time per operation, {largest:,} rows (log scale, lower is better)", OPS,
              [(label, [results[largest][mode][op] for op in OPS]) for mode, (label, _) in MODES.items()],
              "microseconds per operation", log=True)
    bar_chart(OUT / "speedup.svg", "Speed-up with the tree on (tree off time ÷ tree on time, higher is better)", OPS,
              [(label, [results[s]["off"][op] / results[s]["on"][op] for op in OPS]) for s, label in zip(sizes, labels)],
              "times faster", unit="x", ref=1.0)
    bar_chart(OUT / "delete.svg", "Delete time by mode (log scale, lower is better)", labels,
              [(label, [results[s][mode]["Delete"] for s in sizes]) for mode, (label, _) in MODES.items()],
              "microseconds per delete", log=True)
    scaling_chart(
        OUT / "read_scaling.svg", "Lookup time as the table grows (log scales, lower is better)", sizes,
        [(f"{op}, tree {mode}", line + marker, COLORS[m], [results[s][mode][op] for s in sizes])
         for op, line in (("Read (str)", "-"), ("Read (int)", "--")) for m, (mode, marker) in enumerate((("off", "o"), ("on", "s")))],
        "microseconds per lookup")


if __name__ == "__main__":
    main()
