# SiQL (Simple Query Language)
![Version](https://img.shields.io/badge/version-0.1.0-blue)

### *A Python API for local tables and data access/searching much like [SQLite](https://sqlite.org/)*
This Python library is intended to make data access and usage simpler with single calls and query chains

## Project layout
```
SiQL/
├── __init__.py          # public API: Table, Row, RowStruct, Cell, TableDiff, Command, table_file, ErrorHandler
├── __main__.py          # demo: python -m SiQL
├── models/              # data structures
│   ├── row.py           # RowStruct, Cell, Row
│   ├── table.py         # Table (including saving to / loading from its .siql file)
│   └── diff.py          # diff notation: DiffOp subclasses, TableDiff, commit_line
├── controllers/         # query handling
│   └── interpreter.py   # Command, Interpreter
├── managers/            # file management
│   └── table_file.py    # table_file
├── helpers/             # shared utilities
│   ├── error_handler.py # ErrorHandler
│   └── types.py         # column type <-> name conversion for table files
└── tests/
    ├── test_core.py         # package layout, models, diff, manager, controller, helpers
    └── test_persistence.py  # file saving and crash recovery
```

## Running
SiQL is a package, so run commands from the folder that **contains** `SiQL/`, or from inside `SiQL/` as shown:
```
python -m SiQL                                      # demo (from the parent folder)
python -m unittest discover -s tests -t .. -v       # tests (from inside SiQL/)
```
