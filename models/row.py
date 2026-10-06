from typing import Type


class RowStruct():
    def __iter__(self):
        for i in range(len(self.column_names)):
            yield i

    def __len__(self):
        return len(self.columns)

    def __init__(self, **args):
        self.columns:list[Type] = [i for i in args.values()]
        self.column_names:list[str] = [i for i in args.keys()]

    def add(self, **args):
        self.columns.extend([i for i in args.values()])
        self.column_names.extend([i for i in args.keys()])

    def col(self, name: str | int) -> int:
        return self.column_names.index(name) if isinstance(name, str) else name

class Cell():
    """Mutable value holder shared between a table's rows and columns."""
    def __init__(self, value=None):
        self.value = value

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return repr(self.value)


class Row():
    def __init__(self, struct:RowStruct, content = None):
        self.struct = struct
        if content is None:
            content = [None] * len(struct)
        self.content:list[Cell] = [c if isinstance(c, Cell) else Cell(c) for c in content]

    def __str__(self):
        return f"{self.content}"

    def _fill(self):
        # Pad with empty cells for columns added to the struct after this row was created
        while len(self.content) < len(self.struct):
            self.content.append(Cell())

    def col(self, name: str | int) -> Cell:
        """Get cell by column"""
        try:
            i = self.struct.col(name)
        except ValueError:
            raise IndexError("Column not found")
        self._fill()
        return self.content[i]

    def append(self, item, nv):
        """Set cell by column name
        Args:
            item: column name
            nv: new value
        """
        try:
            self.col(item).value = nv
        except IndexError:
            return "Column not found"
