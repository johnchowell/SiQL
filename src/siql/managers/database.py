import os
import re
import shutil

from ..models.table import Table

TABLE_EXT = ".siql"
# Table names become file names, so only plain identifiers are allowed (no paths)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class Database():
    """A folder of table files opened by name: table `people` is stored in `<path>/people.siql`.

    Each open Table keeps its rows in memory, so only one Database should use a folder at a time.
    """
    def __init__(self, path: str = "."):
        self.path = os.path.abspath(path)
        os.makedirs(self.path, exist_ok=True)
        self._open: dict[str, Table] = {}

    def file(self, name: str) -> str:
        """Path of a table's file (whether or not it exists)."""
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError(f"Invalid table name: {name!r}")
        return os.path.join(self.path, name + TABLE_EXT)

    def tables(self) -> list[str]:
        """Names of all tables in the folder."""
        names = (f[:-len(TABLE_EXT)] for f in os.listdir(self.path) if f.endswith(TABLE_EXT))
        return sorted(n for n in names if _NAME.match(n) and os.path.isfile(self.file(n)))

    def __contains__(self, name) -> bool:
        try:
            return os.path.isfile(self.file(name))
        except ValueError:
            return False

    def table(self, name: str) -> Table:
        """Open an existing table. Raises KeyError if it doesn't exist."""
        file = self.file(name)
        key = os.path.normcase(file)
        if key not in self._open:
            if not os.path.isfile(file):
                raise KeyError(f"No such table: {name}")
            self._open[key] = Table(file=file)
        return self._open[key]

    def create(self, name: str, /, **columns) -> Table:
        """Create a table with column names and types, e.g. `db.create("people", name=str, age=int)`."""
        file = self.file(name)
        if os.path.exists(file):
            raise ValueError(f"Table already exists: {name}")
        table = Table(file=file)
        if columns:
            table.addCols(**columns)
        self._open[os.path.normcase(file)] = table
        return table

    def drop(self, name: str):
        """Delete a table and its file. Raises KeyError if it doesn't exist."""
        file = self.file(name)
        if not os.path.isfile(file):
            raise KeyError(f"No such table: {name}")
        self._open.pop(os.path.normcase(file), None)
        os.remove(file)

    def _check_new(self, src: str, dst: str) -> tuple[str, str]:
        old, new = self.file(src), self.file(dst)
        if not os.path.isfile(old):
            raise KeyError(f"No such table: {src}")
        if os.path.exists(new):
            raise ValueError(f"Table already exists: {dst}")
        return old, new

    def rename(self, name: str, new_name: str) -> Table:
        """Rename a table and its file. An open Table object keeps working under the new name."""
        old, new = self._check_new(name, new_name)
        table = self._open.pop(os.path.normcase(old), None)
        os.rename(old, new)
        if table is None:
            return self.table(new_name)
        table._file = new
        self._open[os.path.normcase(new)] = table
        return table

    def copy(self, name: str, new_name: str) -> Table:
        """Copy a table to a new table file and open the copy."""
        old, new = self._check_new(name, new_name)
        shutil.copyfile(old, new)
        return self.table(new_name)
