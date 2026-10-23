"""Search-path timings for two builds of the same fixture, in alternating fresh processes.

Run from the repository root after building both source trees in place (needs matplotlib to plot):
    python benchmarks/search_paths.py --base OLD/src --new src [--repeats 5]
    python benchmarks/search_paths.py --replot
Writes docs/benchmarks/search_paths_results.json and docs/benchmarks/search_paths.svg.
Every workload's results must agree between the builds, or the run stops.
"""
import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "benchmarks"
SIZE = 100_000
QUERIES = 50
INNER = 5


def sample(src: str) -> dict:
    """Median microseconds per operation for each workload in this process, and a digest of each one's results."""
    sys.path.insert(0, src)
    from siql import Database, Interpreter, TableDiff
    from siql.controllers.parser import parse
    from siql.models import AddRow

    rng = random.Random(88)
    names = [chr(65 + i % 26) + f" person {i:03}" for i in range(512)]
    records = [{"name": rng.choice(names), "age": rng.randrange(100), "n": i} for i in range(SIZE)]
    picks = rng.sample(range(SIZE), QUERIES)
    many = rng.sample(range(SIZE), 2000)  # cheap lookups need more probes to rise above timer noise
    folder = tempfile.TemporaryDirectory()
    db = Database(folder.name)

    def table(name, cache):
        t = db.create(name, name=str, age=int, n=int)
        t.cache_max_bytes = cache
        TableDiff([AddRow(i, r) for i, r in enumerate(records)]).apply(t)
        t.find("n", 0)  # the first lookup builds the row-position map
        return t

    t = table("t", 0)
    sql = Interpreter(db)
    inlist = ", ".join(str(v) for v in range(0, 5000, 100))
    queries = {
        "Range, 0.1% of rows": lambda i: "SELECT COUNT(*) FROM t WHERE n < 100",
        "Range, two bounds, 100 rows": lambda i: f"SELECT COUNT(*) FROM t WHERE n >= {i} AND n < {i + 100}",
        "Range, 1% of rows": lambda i: "SELECT COUNT(*) FROM t WHERE age > 98",
        "Equality AND range": lambda i: f"SELECT n FROM t WHERE name = '{records[i]['name']}' AND age > 50",
        "Equality AND equality": lambda i: (f"SELECT n FROM t WHERE name = '{records[i]['name']}' "
                                            f"AND age = {records[i]['age']}"),
        "IN, 50 values": lambda i: f"SELECT COUNT(*) FROM t WHERE n IN ({inlist})",
        "ORDER BY LIMIT 10, whole table": lambda i: "SELECT n FROM t ORDER BY n DESC LIMIT 10",
        "WHERE + ORDER BY LIMIT 5": lambda i: (f"SELECT n FROM t WHERE name = '{records[i]['name']}' "
                                               f"ORDER BY age LIMIT 5"),
    }
    workloads = {
        "Exact, unique": (lambda: [t.find("n", records[i]["n"]) for i in many], len(many)),
        "Exact, ~195 rows": (lambda: [t.find("name", records[i]["name"]) for i in many], len(many)),
        "find_where, 2 columns": (lambda: [t.find_where(name=records[i]["name"], age=records[i]["age"])
                                           for i in many], len(many)),
        "Parse a short query": (lambda: [parse(f"SELECT name FROM t WHERE age > {i % 90} AND name = 'Ann' "
                                               f"ORDER BY age DESC LIMIT 10") for i in many], len(many)),
    }
    for label, text in queries.items():
        workloads[label] = ((lambda text: lambda: [sql.execute(text(i)).rows for i in picks])(text), QUERIES)

    out = {}
    for label, (action, count) in workloads.items():
        results, runs = action(), []
        for _ in range(INNER):
            start = time.perf_counter_ns()
            action()
            runs.append((time.perf_counter_ns() - start) / 1000 / count)
        digest = hashlib.sha256(repr(results).encode()).hexdigest()[:16]
        out[label] = {"us": statistics.median(runs), "digest": digest}

    # One insert after many cached lookups, then a cached key read between unrelated inserts
    cached = table("cached", 100_000_000)
    keys = list(range(0, SIZE, 5))
    inserts = []
    for r in range(INNER):
        for k in keys:
            cached.find("n", k)
        start = time.perf_counter_ns()
        cached.add(name="Z", age=1, n=-1 - r)
        inserts.append((time.perf_counter_ns() - start) / 1000)
    out["Insert after 20,000 cached reads"] = {"us": statistics.median(inserts), "digest": ""}
    for j in range(200):
        cached.find("n", j)
    hits = cached.cache_info["hits"]
    for j in range(200):
        cached.add(name="Y", age=2, n=-10_000 - j)
        cached.find("n", j)
    out["Hit rate, reads between inserts (%)"] = {"us": 100 * (cached.cache_info["hits"] - hits) / 200,
                                                  "digest": ""}
    folder.cleanup()
    return out


def chart(data: dict, path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    surface, ink, muted, grid = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9"
    before_color, after_color = "#86b6ef", "#1c5cab"  # one hue, light then dark: validated ordinal pair
    labels = [k for k in data["base"] if not k.endswith("(%)")]
    labels.sort(key=lambda k: data["base"][k] / data["new"][k])
    before = [data["base"][k] for k in labels]
    after = [data["new"][k] for k in labels]
    fig, ax = plt.subplots(figsize=(10, 6.4))
    fig.patch.set_facecolor(surface)
    ax.set_facecolor(surface)
    ys = range(len(labels))
    for y, b, a in zip(ys, before, after):
        ax.plot([a, b], [y, y], color=grid, linewidth=2, solid_capstyle="round", zorder=1)
    ax.scatter(before, ys, s=64, color=before_color, edgecolors=surface, linewidths=2, zorder=2,
               label="Before (5af331a)")
    ax.scatter(after, ys, s=64, color=after_color, edgecolors=surface, linewidths=2, zorder=3, label="After")
    ax.set_xscale("log")
    right = max(before + after) * 4
    ax.set_xlim(min(before + after) / 2, right * 6)
    for y, b, a in zip(ys, before, after):
        ax.annotate(f"{b / a:,.1f}x" if b >= a else f"{b / a:.2f}x", (max(a, b), y), xytext=(10, 0),
                    textcoords="offset points", va="center", fontsize=9, color=ink)
    ax.set_yticks(list(ys), labels, fontsize=9, color=ink)
    ax.tick_params(axis="x", colors=muted, labelsize=8)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=grid, linewidth=1)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.set_xlabel("microseconds per operation, log scale (lower is better)", color=muted, fontsize=9)
    meta = data["meta"]
    ax.set_title(f"Search paths at {meta['rows']:,} rows: median of {meta['repeats']} alternating processes",
                 loc="left", fontsize=11, color=ink)
    ax.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=ink)
    fig.tight_layout()
    fig.savefig(path, facecolor=surface, metadata={"Date": None})
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", metavar="SRC", help=argparse.SUPPRESS)
    parser.add_argument("--base", help="src folder of the earlier build")
    parser.add_argument("--new", default=str(ROOT / "src"), help="src folder of the build under test")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--replot", action="store_true")
    args = parser.parse_args()
    path = OUT / "search_paths_results.json"
    if args.sample:
        print(json.dumps(sample(args.sample)))
        return
    if args.replot:
        chart(json.loads(path.read_text(encoding="utf-8")), OUT / "search_paths.svg")
        return
    if not args.base:
        parser.error("--base is required unless --replot is given")
    runs = {"base": [], "new": []}
    for repeat in range(args.repeats):
        for build in ("base", "new"):  # alternate so background load affects both builds alike
            src = str(Path(getattr(args, build)).resolve())
            done = subprocess.run([sys.executable, __file__, "--sample", src], capture_output=True, text=True,
                                  check=True)
            runs[build].append(json.loads(done.stdout))
            print(f"repeat {repeat + 1}/{args.repeats} {build} done", flush=True)
    for label in runs["base"][0]:
        digests = {run[label]["digest"] for build in runs for run in runs[build]}
        if len(digests) != 1:
            raise AssertionError(f"{label}: the builds returned different results")
    data = {"meta": {"python": platform.python_version(), "os": platform.platform(),
                     "measured_at": datetime.now(timezone.utc).isoformat(), "rows": SIZE, "queries": QUERIES,
                     "api_probes": 2000, "inner_repeats": INNER, "repeats": args.repeats, "cache_max_bytes": 0,
                     "cache_workloads": "100 MB cache; 20,000 distinct cached lookups before each timed insert",
                     "agreement": "identical result digests across builds and repeats"},
            "base": {k: statistics.median(r[k]["us"] for r in runs["base"]) for k in runs["base"][0]},
            "new": {k: statistics.median(r[k]["us"] for r in runs["new"]) for k in runs["new"][0]},
            "runs": runs}
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    chart(data, OUT / "search_paths.svg")
    for label in data["base"]:
        print(f"{label:40s} {data['base'][label]:12.2f} {data['new'][label]:12.2f}")


if __name__ == "__main__":
    main()
