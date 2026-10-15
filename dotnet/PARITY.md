# Python / C# feature map

New features must update both implementations and add shared cases in `tools/dotnet_parity.py`
or standalone checks in `dotnet/SiQL.Tests/Program.cs`. CI builds both, runs the original Python
suite, and checks shared fixtures. C# uses PascalCase and dictionary arguments in place of kwargs.
The checked-in `python-api.json` contract also rejects unreviewed public Python API changes.
After implementing a feature in both languages and extending the tests, acknowledge its API
change with `python tools/dotnet_parity.py --update-api`, then run the parity suite.

| Python surface | C# counterpart | Evidence |
|---|---|---|
| Table/Row/Cell/RowStruct; live shared cells | Same class names, Rows/Cols/Struct, Col/Value | Identity, observers, mutation traces |
| Initial rows, files, tree toggle | initialRows/file/tree constructor options, Tree | Shared initial identity, replay, rebuild |
| addCols/add/column/select/editByName | AddCols/Add/Column/Select/EditByName | Core traces and library checks |
| find/find_batch/find_where | Find/FindBatch/FindWhere | Seven modes, duplicate and missing queries, input order |
| ColumnTree/Branch/skip list/scans | ColumnTree/Branch/SkipList<T>/Search | Branch-local queues, fingers, traces, scans |
| Lazy add/delete/purge | LazyAdd/LazyDelete/Purge | Immediate visibility, edits, cancellation, rollback |
| Rolling cache, runtime resize, clear/info | QueryCache/CacheMaxBytes/ClearCache/CacheInfo | LRU, budget, oversized results, per-column invalidation |
| Traversal admissions | CacheTraversedNodes | Compared-node cache admissions |
| Prefix grouping and diagnostics | CompressedSearch/CompressedInfo/CompressedIndex | Exact partial matches, mutable binary edits |
| Integer grouping and low/lower seek | IntegerSearch/IntegerInfo/IntegerIndex | Identical packed keys, exact results and bounds |
| All seven DiffOp types | AddCol/DropCol/SetColType/RenameCol/AddRow/DropRow/SetCell | Exact logs and round-trips |
| TableDiff between/loads/dumps/write/apply | Between/Loads/Dumps/Write/Apply | 100 exact SequenceMatcher-compatible diff fixtures |
| Rollback, save, compact, crash recovery | Same operations and log format | Rollback file unchanged, truncated tail, bidirectional files |
| Database/table_file | Database/TableFile | Create/open/list/rename/copy/drop, diff helper |
| Parser, tokenizer, Expr/Statement types, Command | Parser/Token/Expr and Statement records/Command | SQL corpus, Unicode, syntax rejection |
| All SQL statement families | Interpreter.Execute/ExecuteScript/Run | Rows, columns, row counts, messages, error cases |
| Result/to_dict/text formatting | Result/ToDictionary/ToString/TextFormat.Box | Shared results, console output |
| Literal values, type names, equality | ColumnType/Values/PythonLiteral | Numeric equality, nested literals, Unicode, bytes, complex |
| CSV inference/import/export | Csv and Interpreter file operations | Round-trip, inference, invalid-input rollback |
| HTTP server/config/token/body limits | SiQLServer/ServerConfig | HTTP integration checks and configuration |
| TOML/SIQL_TOKEN/systemd/CLI/demo | ServerConfig.Load/Service.SystemdUnit/SiQL.Cli | Compiled CLI and package smoke tests |
| ErrorHandler | ErrorHandler | Same placeholder public type |

CLR types/exceptions, assembly layout, launcher syntax, allocation sizes, and the absence of
the Python GIL are runtime differences. Search defaults, file/data semantics, SQL, and feature
switches are shared. Python classes are not CLR classes; applications supply native CLR values
and custom `Equals` implementations rather than Python callbacks.
