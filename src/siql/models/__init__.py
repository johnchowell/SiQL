"""Data models: table structure, rows/cells, and the diff notation used to persist changes."""
from .row import RowStruct, Cell, Row
from .diff import DiffOp, AddCol, DropCol, SetColType, RenameCol, AddRow, DropRow, SetCell, TableDiff, commit_line
from .tree import ColumnTree, tree_key
from .table import Table
from functools import wraps


# unittest.mock autospec only creates a binding descriptor for Python functions.
# Preserve that public behavior while forwarding all work to the native method.
_native_find = Table.find


@wraps(_native_find)
def _find(self, col_name, value):
    return _native_find(self, col_name, value)


Table.find = _find

__all__ = [
    "RowStruct", "Cell", "Row", "Table", "ColumnTree", "tree_key",
    "DiffOp", "AddCol", "DropCol", "SetColType", "RenameCol", "AddRow", "DropRow", "SetCell", "TableDiff",
    "commit_line",
]
