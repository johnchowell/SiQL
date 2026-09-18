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

class Row():
    def __init__(self, struct:RowStruct, content = None):
        self.struct = struct
        self.content = [None] * len(struct) if content is None else content

    def __str__(self):
        return f"{self.content}"

    def col(self, name:str):
        c = self.content[self.struct.col(name)]
        return Row(RowStruct(**{f"{type(c)}": name}), c)

    def append(self, item, nv):
        try:
            i = self.struct.column_names.index(item)
            self.content[i] = nv
        except ValueError:
            return "Column not found"


class Table():
    entries:list[Row]
    struct:RowStruct

    def column(self, name: str | int):
        for i in self.entries:
            yield i.col(name)

    # Builds a nice little print table
    def __str__(self):
        names = self.struct.column_names
        if not names:
            return ""

        head = [f"{n}: {t.__name__}" for n, t in zip(names, self.struct.columns)]
        body = [[str(row.col(j)) for j in range(len(names))] for row in self.entries]
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

    def __init__(self, t:list[Row] = []):
        self.entries = t
        self.struct = RowStruct() if len(t) == 0 else t[0].struct

    def select(self, var, col_name=None):
        if col_name is None:
            t:Type = type(var)
            for i in self.struct:
                if self.struct.columns[i] == t:
                    for j in self.entries:
                        if var in j.content:
                            yield j
        else:
            for j in self.column(col_name):
                if var in j.content:
                    yield j

    def addCols(self, **args):
        self.struct.add(**args)

    def editByName(self, col, where, change):
        for i in self.column(col):
            i = change if i == where else None

    def add(self, **args):
        r = Row(self.struct)
        for item in args.items():
            r.append(item[0], item[1])
        self.entries.append(r)

class TChain(Table):
    def __init__(self):
        super().__init__()

if __name__ == "__main__":
    t = Table()
    t.addCols(string=str, fart=bool, toot=int)
    t.add(string="doodoo", fart=True, toot=12)
    t.add(string="shid")
    t.add(string="graaaaaaaaaaaaa", fart=False, toot=0)
    t.add(s="a")

    for i in t.select("shid", "string"):
        print(i)

    for i in t.select("shid"):
            print(i)

    print(t)

