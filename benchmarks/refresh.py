"""Refresh every benchmark dataset and graph sequentially (run using WSL's native Python).

Requires the Windows .venv-native Python and Matplotlib in this Linux environment.
Logs each phase under build/benchmark-refresh; measurements use full default workloads.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/benchmarks'
LOGS = ROOT / 'build/benchmark-refresh'


def phase(name, args):
    print(f'START {name}', flush=True)
    with (LOGS / (name + '.log')).open('w', encoding='utf-8') as log:
        subprocess.run(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    print(f'DONE {name}', flush=True)


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    linux = sys.executable
    windows = str(ROOT / '.venv-native/Scripts/python.exe')
    phase('native-audit', [linux, 'tools/check_native.py'])
    # Keep maintained Python source isolated from editable native modules, retaining the identical C engine.
    from siql import _speedups
    with tempfile.TemporaryDirectory(prefix='siql-python-benchmark-') as temporary:
        reference = Path(temporary)
        shutil.copytree(ROOT / 'src/siql', reference / 'siql',
                        ignore=lambda directory, names: [name for name in names
                                                        if name.endswith(('.so', '.pyd')) or name == '__pycache__'])
        shutil.copy2(_speedups.__file__, reference / 'siql' / Path(_speedups.__file__).name)
        snippet = ('import sys; sys.path.insert(0, sys.argv[1]); import siql; '
                   'sys.path.insert(0, "benchmarks"); import crud; from pathlib import Path; '
                   'crud.OUT=Path(sys.argv[2]); sys.argv=["crud.py"]; crud.main()')
        phase('python-crud', [linux, '-c', snippet, str(reference), str(reference / 'results')])
        shutil.copy2(reference / 'results/results.json', OUT / 'results-python.json')
    phase('native-crud', [linux, 'benchmarks/crud.py'])
    phase('native-comparison', [linux, 'benchmarks/compare_native.py'])
    phase('batch', [linux, 'benchmarks/batch.py'])
    phase('linux-cache-sqlite', [linux, 'benchmarks/cache_sqlite.py'])
    phase('windows-cache-sqlite', [windows, 'benchmarks/cache_sqlite.py', '--no-plots',
                                  '--output', 'docs/benchmarks/windows_cache_tests'])
    phase('windows-cache-plots', [linux, 'benchmarks/cache_sqlite.py', '--replot',
                                 '--output', 'docs/benchmarks/windows_cache_tests'])
    for name in ('traversal_cache', 'compressed_search', 'integer_search', 'lazy_add'):
        phase(name, [windows, 'benchmarks/' + name + '.py'])
        phase(name + '-plots', [linux, 'benchmarks/' + name + '.py', '--replot'])
    # These directories were previously refreshed independently; keep all copies consistent.
    for directory, names in [('current_crud', ['results.json', 'crud.svg', 'delete.svg', 'pending.svg',
                                              'read_scaling.svg', 'speedup.svg', 'storage.svg']),
                             ('current_batch', ['batch_results.json', 'batch_neighbors.svg', 'batch_random.svg',
                                                'batch_speedup.svg'])]:
        (OUT / directory).mkdir(exist_ok=True)
        for name in names:
            shutil.copy2(OUT / name, OUT / directory / name)
    phase('readme', [linux, 'benchmarks/update_readme.py'])
    manifest = {'completed_at': datetime.now(timezone.utc).isoformat(),
                'method': 'full workloads, sequential runs; Python and native use identical maintained source and C engine',
                'graphs': sorted(str(path.relative_to(OUT)) for path in OUT.rglob('*.svg'))}
    (OUT / 'refresh_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print('ALL BENCHMARKS REFRESHED', flush=True)


if __name__ == '__main__':
    main()
