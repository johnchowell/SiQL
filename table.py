from typing import Type, Self

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


class Table():
    rows:list[Row]
    cols:dict[str, list[Cell]]
    struct:RowStruct

    @property
    def entries(self) -> list[Row]:
        return self.rows

    def column(self, name: str | int):
        """Yield cells in a column
        Args:
            name: column name or index
        """
        if isinstance(name, int):
            name = self.struct.column_names[name]
        yield from self.cols[name]

    # Builds a nice little print table
    def __str__(self):
        names = self.struct.column_names
        if not names:
            return ""

        head = [f"{n}: {t.__name__}" for n, t in zip(names, self.struct.columns)]
        body = [[str(row.col(j)) for j in range(len(names))] for row in self.rows]
        width = [max(len(head[j]), *(len(r[j]) for r in body), 0) for j in range(len(names))]

        def rule(left, mid, right):
            return left + mid.join("─" * (w + 2) for w in width) + right

        def line(cells):
            return "│" + "│".join(f" {v:<{w}} " for v, w in zip(cells, width)) + "│"

        sep = rule("├", "┼", "┤")
        out = [rule("┌", "┬", "┐"), line(head), sep]
        for n, r in enumerate(body):
            if n:
                out.append(sep)
            out.append(line(r))
        out.append(rule("└", "┴", "┘"))
        return "\n".join(out)

    def __init__(self, t:list[Row] | None = None):
        self.rows = [] if t is None else t
        self.struct = RowStruct() if len(self.rows) == 0 else self.rows[0].struct
        # Each column list holds the same Cell objects as the rows, so edits are visible both ways
        self.cols = {name: [r.col(name) for r in self.rows] for name in self.struct.column_names}

    def select(self, var, col_name=None):
        """Select rows by value
        Args:
            var: value to match
            col_name: column to check
        """
        if col_name is None:
            t:Type = type(var)
            for i in self.struct:
                if self.struct.columns[i] == t:
                    for j in self.rows:
                        if var == j.col(i).value:
                            yield j
        else:
            for row, cell in zip(self.rows, self.column(col_name)):
                if var == cell.value:
                    yield row

    def addCols(self, **args):
        """Add new columns
        Args:
            **args: column names and types
        """
        self.struct.add(**args)
        for name in args:
            self.cols[name] = [r.col(name) for r in self.rows]

    def editByName(self, col, where, change):
        """Edit cells by column name and value. Will edit multiple cells if they fit the condition.
        Args:
            col: column name
            where: value to match
            change: new value
        """
        for cell in self.column(col):
            if cell.value == where:
                cell.value = change

    def add(self, **args):
        """Add columns with keyword args as name=type
        Args:
            **args: column names and types
        Returns:
            None
        Example:
            `table.add(address=str, object=object)`
        """
        r = Row(self.struct)
        for item in args.items():
            r.append(item[0], item[1])
        self.rows.append(r)
        for name in self.struct.column_names:
            self.cols[name].append(r.col(name))

if __name__ == "__main__":
    t = Table()
    t.addCols(name=str, address=str, phone=str)
    t.add(name="John Doe", address="123 Place St.", phone="(592) 010-2345")
    t.add(name="Jimmy Doe", address="123 Place St. (Basement)")
    t.addCols(email=str)
    t.editByName("email", None, "email@placeholder.org")

    for i in t.select("John Doe"):
        print(i)

    for i in t.select("Jimmy Doe", "name"):
        print(i)

    print(t)

    #Security test

