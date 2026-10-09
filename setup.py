"""Compile the complete implementation, keeping only package/CLI glue in Python."""
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup
from setuptools.command.build_py import build_py


SOURCE_ROOT = Path("src")
IMPLEMENTATION = sorted(
    path for path in (SOURCE_ROOT / "siql").rglob("*.py")
    if "tests" not in path.parts and path.name not in {"__init__.py", "__main__.py", "demo.py"}
)
MODULE_NAMES = {".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts) for path in IMPLEMENTATION}


class NativeBuildPy(build_py):
    """Installed distributions contain extensions rather than interpreted duplicates."""

    def find_package_modules(self, package, package_dir):
        return [
            item for item in super().find_package_modules(package, package_dir)
            if f"{item[0]}.{item[1]}" not in MODULE_NAMES
        ]


extensions = [
    Extension(".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts), [path.as_posix()], language="c++")
    for path in IMPLEMENTATION
]
extensions = cythonize(
    extensions,
    build_dir="build/cython",
    compiler_directives={
        "language_level": 3,
        # Hints document the public API; callers may pass compatible subclasses and other objects.
        "annotation_typing": False,
        "binding": True,
        "always_allow_keywords": True,
    },
)
extensions.append(Extension("siql._speedups", ["src/siql/_speedups.c"], py_limited_api=True))

setup(ext_modules=extensions, cmdclass={"build_py": NativeBuildPy})
