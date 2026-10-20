# Standalone C++ core: current scope

`siql_native` is a handwritten C++20 library with a C ABI. It builds without
Python or .NET headers or runtimes. The small [ctypes adapter](../src/siql/native.py)
lets Python exercise the bundled DLL/shared object. The existing `siql.Table` still uses
the Cython implementation; this core has **not** replaced the complete library.

The core currently provides in-memory rows, per-column exact hash indexes,
immediate single-row and batch changes, row-position tracking across deletes,
an exact-result rolling LRU cache (100 MB default and runtime resizing),
and `.siql` diff-log replay. It reads all seven operation codes for supported
scalar cells and writes add-column, add-row, set-cell, and drop-row operations.
String, bytes, null, bool, signed 64-bit integer, and finite floating-point
cells round-trip with the Python implementation. Numeric keys compare exactly
across bool/int/float types, including values around 2^53. Invalid UTF-8 and
unsupported values produce errors. Calls on a table handle are synchronized;
callers must finish using a handle before closing it. Values borrowed from
`siql_get` must be copied before another thread mutates the table.

Build on Linux with an installed C++20 compiler:

```sh
g++ -std=c++20 -O3 -fPIC -shared -Inative/include native/src/siql_native.cpp -o build/libsiql_native.so
g++ -std=c++20 -O2 -Inative/include native/tests/native_tests.cpp -Lbuild -lsiql_native -o build/siql_native_tests
LD_LIBRARY_PATH=build build/siql_native_tests
```

On Windows, from a Visual Studio developer command prompt:

```bat
cmake -S native -B build/native-win -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build/native-win
python native/tests/cross_language.py -v
```

The C ABI is declared in [siql_native.h](include/siql_native.h). The Python wheel
bundles the DLL/shared object and exposes `siql.native.NativeTable`. A local
build also finds the core under `build/`; `SIQL_NATIVE_LIBRARY` can override
either location:

```python
from siql.native import NativeTable

table = NativeTable('example.siql')
table.add_column('name', 'str')
table.append(name='Ann')
print(table.find('name', 'Ann'))  # [0]
table.close()
```

Remaining work for a complete replacement is substantial: arbitrary Python
literal and custom cell values; live `Row`/`Cell` identity and observers;
traversed-node cache admissions; lazy add/delete; compressed and integer composite indexes;
ordered and chained searches; full diff construction and transaction rollback;
database management; SQL parser/interpreter; CSV; HTTP server; and CLI. The C#
library is still its own managed implementation. These differences are why the
new C++ core has a separate adapter and benchmark label today. The existing
188 Python tests must pass against the replacement before the default can switch.
