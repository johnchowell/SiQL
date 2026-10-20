"""Summarize repeated tree-reference benchmarks and regenerate the comparison graph.

Run benchmarks/tree_refs.py five times per mode in alternating order, save the
JSON lines as build/tree_refs_runs.jsonl, then run this script.
"""
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "build" / "tree_refs_runs.jsonl"
OUT = ROOT / "docs" / "benchmarks"
METRICS = ("build_ms", "memory_bytes", "unique_us", "fanout_us",
           "batch_unique_us", "batch_fanout_us", "edit_us", "index_remove_us")


def main():
    runs = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines()]
    assert {run["refs"] for run in runs} == {"0", "1"}
    assert all(run["rows"] == 30_000 and run["queries"] == 3_000 for run in runs)
    median = {mode: {metric: statistics.median(run[metric] for run in runs if run["refs"] == mode)
                     for metric in METRICS} for mode in ("0", "1")}
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "workload": "30,000 rows, two columns, 3,000 queries, 256 duplicate groups, cache disabled",
        "method": "Five fresh-process runs per mode, alternated; query timings each median of five passes",
        "median": median,
        "runs": runs,
    }
    (OUT / "tree_refs_results.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    labels = ["Unique", "Duplicate", "Batch unique", "Batch duplicate"]
    keys = ["unique_us", "fanout_us", "batch_unique_us", "batch_fanout_us"]
    x = range(len(keys))
    width = 0.36
    fig, ax = plt.subplots(figsize=(8, 4))
    for offset, mode, name, color in [
        (-width / 2, "0", "Tree", "#72819c"),
        (width / 2, "1", "Tree + direct references", "#218373"),
    ]:
        ax.bar([i + offset for i in x], [median[mode][key] for key in keys],
               width, label=name, color=color)
    ax.set_xticks(list(x), labels)
    ax.set_yscale("log")
    ax.set_ylabel("µs per query (lower is better)")
    ax.set_title("Cold exact search at 30,000 rows")
    ax.legend()
    fig.tight_layout()
    svg = OUT / "tree_refs.svg"
    fig.savefig(svg)
    plt.close(fig)
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines()) + "\n",
                   encoding="utf-8")
    print(json.dumps(median, indent=2))


if __name__ == "__main__":
    main()
