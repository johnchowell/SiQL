from ..models.table import Table
from ..models.diff import TableDiff


class table_file:
    table:Table
    def __init__(self):
        self.table = Table()

    def diff(self, table:Table) -> TableDiff:
        """Return the changes needed to turn this file's table into `table`.
        Apply them with `diff.apply(self.table)` (which also logs them to its file) or write them with `diff.write(...)`.
        """
        return TableDiff.between(self.table, table)
