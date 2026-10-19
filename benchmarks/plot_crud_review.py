"""Plot retained before/after measurements and update the README review table."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/benchmarks/crud_review'
OPS = ['Create', 'Read (str)', 'Read (int)', 'Update', 'Delete + lookup', 'Batch create']
data = {runtime: {phase: json.loads((OUT / f'{runtime}_{phase}.json').read_text())
                  for phase in ['before', 'after']} for runtime in ['python', 'dotnet']}
plt.rcParams.update({'svg.fonttype': 'none', 'font.size': 10,
                     'axes.spines.top': False, 'axes.spines.right': False})
fig, axes = plt.subplots(1, 2, figsize=(12, 5.4), layout='constrained')
rows = []
for ax, (runtime, label) in zip(axes, [('python', 'Native Python'), ('dotnet', 'C# / .NET 8')]):
    before = data[runtime]['before']['results']['50000']['median_us']
    after = data[runtime]['after']['results']['50000']['median_us']
    for offset, phase, color in [(-.18, 'before', '#8996a5'), (.18, 'after', '#237c9e')]:
        values = data[runtime][phase]['results']['50000']['median_us']
        bars = ax.barh([i + offset for i in range(len(OPS))], [values[op] for op in OPS],
                       height=.32, label=phase.capitalize(), color=color)
        ax.bar_label(bars, labels=[f'{values[op]:,.2f}' for op in OPS], padding=4, fontsize=8)
    ax.set_yticks(range(len(OPS)), OPS)
    ax.invert_yaxis()
    ax.set_xscale('log')
    ax.set_xlim(right=max(before.values()) * 3)
    ax.set_xlabel('Microseconds per operation · log scale · lower is better')
    ax.set_title(label, fontweight='bold')
    ax.grid(axis='x', alpha=.2)
    ax.set_axisbelow(True)
    ax.legend(loc='lower right', frameon=False)
    for op in OPS:
        rows.append(f'| {label} | {op} | {before[op]:,.2f} | {after[op]:,.2f} | {before[op] / after[op]:.2f}× |')
fig.suptitle('CRUD review · 50,000 rows · median of 5 runs · cache disabled', fontweight='bold')
path = OUT / 'comparison.svg'
fig.savefig(path)
(ROOT / 'build').mkdir(exist_ok=True)
fig.savefig(ROOT / 'build/crud-review-preview.png', dpi=140)
plt.close(fig)
path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines()) + '\n', encoding='utf-8')
table = ('| Runtime | Operation | Before (µs/op) | After (µs/op) | Before / after |\n'
         '|---|---|---:|---:|---:|\n' + '\n'.join(rows))
readme = ROOT / 'README.md'
text = readme.read_text(encoding='utf-8')
start, end = '<!-- CRUD_REVIEW_START -->', '<!-- CRUD_REVIEW_END -->'
begin, finish = text.index(start) + len(start), text.index(end)
text = text[:begin] + '\n\n' + table + '\n\n' + text[finish:]
readme.write_text(text, encoding='utf-8')
print(table)
