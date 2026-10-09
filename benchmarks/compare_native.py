"""Plot same-machine Python + C-search versus full native benchmark results."""
import json

from crud import OUT, OPS, bar_chart


def main():
    python = json.loads((OUT / "results-python.json").read_text(encoding="utf-8"))
    native = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
    for key in ("python", "sqlite", "os", "machine", "lookups", "repeats"):
        if python["meta"][key] != native["meta"][key]:
            raise ValueError(f"Benchmark environments differ: {key}")
    if not python["meta"]["c_speedups"] or not native["meta"]["c_speedups"]:
        raise ValueError("Both implementations must use the same C search engine")
    sizes = sorted(native["results"], key=int)
    if sizes != sorted(python["results"], key=int):
        raise ValueError("Benchmark table sizes differ")
    largest = sizes[-1]
    mode_labels = [("on", "tree on"), ("lazy", "lazy delete")]
    bar_chart(
        OUT / "native_comparison.svg",
        f"Python vs native C++, {int(largest):,} rows (log scale, lower is better)", OPS,
        [(f"{label}, {mode_label}", [data["results"][largest][mode][op] for op in OPS])
         for data, label in ((python, "Python + C search"), (native, "Native C++"))
         for mode, mode_label in mode_labels],
        "microseconds per operation", log=True,
        colors=["#9aa5b1", "#66717e", "#2b7bb9", "#e07b39"],
    )
    bar_chart(
        OUT / "native_speedup.svg",
        f"Native speed-up, {int(largest):,} rows (Python time ÷ native time, higher is better)", OPS,
        [(label, [python["results"][largest][mode][op] / native["results"][largest][mode][op] for op in OPS])
         for mode, label in (("off", "tree off"), ("on", "tree on"), ("lazy", "tree on + lazy delete"))],
        "times faster", unit="ratio", ref=1.0,
    )


if __name__ == "__main__":
    main()
