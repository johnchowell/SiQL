# SiQL for .NET

Standalone C# implementation for .NET 8 or later. The table engine, indexes, interpreter, and
HTTP server run in managed code. CPython, Python.NET, and Python extensions are not runtime
dependencies. [Tomlyn](https://github.com/xoofx/Tomlyn) supplies TOML parsing.

```powershell
dotnet build dotnet/SiQL/SiQL.csproj -c Release
dotnet pack dotnet/SiQL/SiQL.csproj -c Release -p:PackageOutputPath=../../dist/dotnet
dotnet pack dotnet/SiQL.Cli/SiQL.Cli.csproj -c Release -p:PackageOutputPath=../../dist/dotnet
```

Add `SiQL.NET` version `1.0.0` from the local `dist/dotnet` NuGet source, retaining nuget.org
for managed dependencies, or reference `dotnet/SiQL/SiQL.csproj`. The DLL is
`dotnet/SiQL/bin/Release/net8.0/SiQL.dll`. Use NuGet or deploy its dependencies alongside it.

```csharp
using SiQL;

var table = new Table(file: "people.siql", lazyAdd: true, lazyDelete: true);
table.AddCols(new Dictionary<string, ColumnType>
{
    ["name"] = ColumnType.Str,
    ["age"] = ColumnType.Int
});
table.Add(new Dictionary<string, object?> { ["name"] = "Ann", ["age"] = 31 });
var matches = table.Find("name", "Ann");
new TableDiff([new SetCell(0, "age", 32)]).Apply(table);
table.CacheMaxBytes = 25_000_000;
table.IntegerSearch = true;
matches = table.FindWhere(new Dictionary<string, object?> { ["name"] = "Ann", ["age"] = 32 });
table.Purge();
table.Compact();

var db = new Interpreter("data");
db.Execute("CREATE TABLE people (name str, age int)");
db.Execute("INSERT INTO people VALUES ('Ann', 31), ('Bob', 25)");
Console.WriteLine(db.Execute("SELECT * FROM people WHERE age >= 30"));
```

Names use PascalCase; dictionaries replace Python kwargs. Defaults match Python: tree on,
lazy add/delete off, optional composite indexes off, traversal admissions off, and a
100,000,000-byte rolling cache. Single-column searches keep the ordinary tree. Integer mode
takes precedence over compressed mode. Batch results preserve input order; row positions are
zero-based and current. Direct `Cell.Value` edits update indexes without saving, just as in
Python; use `SetCell` through `TableDiff` for persisted edits.

Python integers map to CLR integer types or `BigInteger`, floats to `double`, bytes to `byte[]`,
tuples to `LiteralTuple`, lists to `IList`, dictionaries to `IDictionary`/`LiteralDictionary`,
sets to `HashSet<object?>`, and complex numbers to `System.Numerics.Complex`. Managed binary
arrays are mutable, bypass result caching, and are checked for in-place composite-index edits.
Custom live objects use CLR `Equals` and wildcard routing. Python classes/callbacks cannot run
inside a standalone CLR process. Persistence accepts supported literal values, not arbitrary
custom objects. Managed cache accounting differs from CPython object sizes while enforcing the
same runtime byte budget.

Both implementations read and write the existing Python-literal `.siql` operation log.
Fixtures cover nested values, NULL dictionary keys, Unicode, bytes, big integers, tuples, sets,
complex numbers, compaction, and partial trailing commits. Give each database folder one owner;
do not open it in Python and C# simultaneously.

## SQL, CLI, and HTTP

All Python SQL statement families are present: CREATE/DROP/ALTER, INSERT, SELECT/COUNT/ORDER/
LIMIT/OFFSET, UPDATE/DELETE, SHOW/DESCRIBE, RENAME/COPY/TRUNCATE, COMPACT, USE, and CSV IMPORT/
EXPORT. Expressions include comparisons, NULL, IN, LIKE, AND/OR/NOT, parentheses, quoted names,
literals, and comments.

```powershell
dotnet run --project dotnet/SiQL.Cli -c Release -- -d data "SHOW TABLES"
dotnet run --project dotnet/SiQL.Cli -c Release -- init -c siql.toml
dotnet run --project dotnet/SiQL.Cli -c Release -- serve -c siql.toml
dotnet run --project dotnet/SiQL.Cli -c Release -- systemd -c siql.toml --user siql -o siql.service
```

The CLI also supports script files, piped input, an interactive shell, and `demo`. The tool
package is `SiQL.NET.Cli`, command `siql-dotnet`. HTTP endpoints and JSON shapes match Python:
GET `/health`, GET `/tables`, POST `/query`. Bearer authentication, body-size limits, completed
results on failure, parsing before execution, and disabled USE/IMPORT/EXPORT are preserved.
The CLI honors `SIQL_TOKEN`. `ServerConfig`, `SiQLServer.ServeAsync`, and `Service.SystemdUnit`
are public embedding APIs. Tables remain unsynchronized; the server serializes database access.
Shared tables need an external lock. There is no Python GIL, but no automatic parallel-search
scheduler has been added.

## Verification

```powershell
dotnet run --project dotnet/SiQL.Tests -c Release
.venv-native/Scripts/python.exe tools/dotnet_parity.py
.venv-native/Scripts/python.exe -m unittest discover -s src/siql/tests -t src
```

The first command tests .NET without Python. The parity command uses Python only to produce
fixtures, runs .NET, then reads C#-written files back in Python. It compares 281 SQL cases,
2,800 mutation steps across seven modes, 100 exact diff sequences, literal round-trips, and
the actual packed integer keys. CI runs these alongside the unchanged Python tests on Windows
and Linux. Fixtures demonstrate agreement for tested cases, not a proof about every possible
program. See [PARITY.md](PARITY.md) for the feature map. The
[CRUD review](../docs/CRUD_PERFORMANCE.md) includes separate C# before/after measurements.
The older Python/C++ graphs must not be interpreted as C# measurements.
