"""Run the unchanged repository tests against an isolated, installed native wheel."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from check_native import MODULES, main as check_native


ROOT = Path(__file__).resolve().parents[1]


def test_installed(directory):
    sys.path.insert(0, directory)
    import siql
    from siql.native import NativeTable
    assert Path(siql.__file__).resolve().is_relative_to(Path(directory).resolve())
    check_native()
    with tempfile.TemporaryDirectory(prefix="siql-native-wheel-") as folder:
        path = Path(folder) / "smoke.siql"
        table = NativeTable(path)
        table.add_column("value", "int")
        table.append(value=7)
        assert table.find("value", 7) == [0]
        table.close()
        assert siql.Table(file=str(path)).rows[0].col("value").value == 7
    # Extend only the top-level package to locate the original, unmodified tests.
    siql.__path__.append(str(ROOT / "src" / "siql"))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "src" / "siql" / "tests"), top_level_dir=str(ROOT / "src"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--installed":
        return test_installed(sys.argv[2])
    wheel = Path(sys.argv[1]).resolve()
    with zipfile.ZipFile(wheel) as archive:
        native_modules = set()
        library = "siql_native.dll" if os.name == "nt" else (
            "libsiql_native.dylib" if sys.platform == "darwin" else "libsiql_native.so")
        assert f"siql/{library}" in archive.namelist(), library
        assert "siql/native.py" in archive.namelist()
        assert not any(name.startswith("siql/siql_native.") and name.endswith((".lib", ".exp"))
                       for name in archive.namelist())
        for name in archive.namelist():
            if name.startswith("siql/") and name.endswith((".pyd", ".so")):
                if name == f"siql/{library}":
                    continue
                parts = name.split("/")[1:]
                parts[-1] = parts[-1].split(".")[0]
                native_modules.add(".".join(parts))
        assert native_modules == set(MODULES), native_modules.symmetric_difference(MODULES)
        for name in MODULES:
            assert f"siql/{name.replace('.', '/')}.py" not in archive.namelist(), name
    with tempfile.TemporaryDirectory(prefix="siql-wheel-") as directory:
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", directory, str(wheel)], check=True)
        env = dict(os.environ, SIQL_REQUIRE_SPEEDUPS="1")
        return subprocess.call([sys.executable, str(Path(__file__).resolve()), "--installed", directory], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
