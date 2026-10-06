# SiQL (Simple Query Language)
![Version](https://img.shields.io/badge/version-0.1.0-blue)

### *A Python API for local tables and data access/searching much like [SQLite](https://sqlite.org/)*
This Python library is intended to make data access and usage simpler with single calls and query chains

## Installation
Requires Python 3.11+.
```
pip install SiQL
```

## Project layout
```
src/SiQL/
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
```
python -m SiQL                                          # demo (after installing)
pip install -e .                                        # editable install for development
python -m unittest discover -s src/SiQL/tests -t src -v # tests (from the repository root)
```

## Releasing
```
pip install build twine
python -m build             # creates dist/*.tar.gz and dist/*.whl
twine check dist/*
twine upload dist/*
```

## License
[MIT](https://github.com/johnchowell/SiQL/blob/master/LICENSE)
