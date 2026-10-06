__version__ = "1.0.0"

from .models import Table, Row, RowStruct, Cell, TableDiff
from .managers import table_file, Database
from .helpers import ErrorHandler

# The query interpreter is optional: it's only imported the first time one of these is used
_LAZY = {"Command": ".controllers", "Interpreter": ".controllers", "QueryError": ".controllers"}


def __getattr__(name):
    if name in _LAZY:
        from importlib import import_module
        return getattr(import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Table", "Row", "RowStruct", "Cell", "TableDiff", "table_file", "Database", "ErrorHandler", *_LAZY]