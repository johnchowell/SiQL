"""Build the Cython implementation and bundle the independent C++ scalar core."""
import os
import shutil
import sys
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.build_py import build_py


SOURCE_ROOT = Path("src")
IMPLEMENTATION = sorted(
    path for path in (SOURCE_ROOT / "siql").rglob("*.py")
    if "tests" not in path.parts and path.name not in {"__init__.py", "__main__.py", "demo.py", "native.py"}
)
MODULE_NAMES = {".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts) for path in IMPLEMENTATION}


class NativeBuildPy(build_py):
    """Installed distributions contain extensions rather than interpreted duplicates."""

    def find_package_modules(self, package, package_dir):
        return [
            item for item in super().find_package_modules(package, package_dir)
            if f"{item[0]}.{item[1]}" not in MODULE_NAMES
        ]


class NativeBuildExt(build_ext):
    """Build the standalone C ABI library alongside CPython extensions."""

    def run(self):
        super().run()
        if os.name == "nt":
            library = "siql_native.dll"
            options = ["/std:c++20", "/EHsc"]
        else:
            library = "libsiql_native.dylib" if sys.platform == "darwin" else "libsiql_native.so"
            options = ["-std=c++20"]
        objects = self.compiler.compile(
            ["native/src/siql_native.cpp"], output_dir=self.build_temp,
            include_dirs=["native/include"], macros=[("SIQL_NATIVE_BUILD", "1")],
            extra_postargs=options,
        )
        destination = Path(self.build_lib) / "siql" / library
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.compiler.link_shared_object(objects, str(destination), target_lang="c++")
        if os.name == "nt":
            for suffix in (".exp", ".lib"):
                destination.with_suffix(suffix).unlink(missing_ok=True)
        if self.inplace:
            shutil.copy2(destination, SOURCE_ROOT / "siql" / library)


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

setup(ext_modules=extensions, cmdclass={"build_py": NativeBuildPy, "build_ext": NativeBuildExt})
