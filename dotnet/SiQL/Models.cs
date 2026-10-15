using System.Collections;
using System.Numerics;
using System.Text;

namespace SiQL;

public sealed class RowStruct : IEnumerable<int>
{
    internal readonly List<string> Names = [];
    internal readonly List<ColumnType> Types = [];
    public IReadOnlyList<string> ColumnNames => Names.AsReadOnly();
    public IReadOnlyList<ColumnType> Columns => Types.AsReadOnly();
    public int Count => Names.Count;
    public RowStruct()
    {
    }
    public RowStruct(IReadOnlyDictionary<string, ColumnType> columns)
    {
        foreach (var (name, type) in columns)
            Add(name, type);
    }
    public void Add(string name, ColumnType type)
    {
        Names.Add(name);
        Types.Add(type);
    }
    public void Add(IReadOnlyDictionary<string, ColumnType> columns)
    {
        foreach (var (name, type) in columns)
            Add(name, type);
    }
    public int Col(int index) => index;
    public IEnumerator<int> GetEnumerator() => Enumerable.Range(0, Count).GetEnumerator();
    IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    public int Col(string name)
    {
        int i = Names.IndexOf(name);
        return i < 0 ? throw new KeyNotFoundException(name) : i;
    }
}

public sealed class Cell(object? value = null)
{
    object? current = value;
    internal ColumnTree? Tree;
    public object? Value
    {
        get => current; set
        {
            object? old = current;
            current = value;
            Tree?.Changed(this, old);
        }
    }
    public override string ToString() => Value is string s ? s : PythonLiteral.Write(Value);
}

public sealed class Row
{
    public RowStruct Struct
    {
        get;
    }
    internal readonly List<Cell> Cells;
    public IReadOnlyList<Cell> Content => Cells.AsReadOnly();
    public byte[]? CompressedSignature
    {
        get; internal set;
    }
    public BigInteger? IntegerSignature
    {
        get; internal set;
    }
    public Row(RowStruct structure, IEnumerable<object?>? content = null)
    {
        Struct = structure;
        Cells = content?.Select(v => v as Cell ?? new Cell(v)).ToList() ?? [];
        Fill();
    }
    internal void Fill()
    {
        while (Cells.Count < Struct.Count)
            Cells.Add(new Cell());
    }
    public Cell Col(string name) => Col(Struct.Col(name));
    public Cell Col(int index)
    {
        Fill();
        return Cells[index < 0 ? Cells.Count + index : index];
    }
    public void Append(string name, object? value) => Col(name).Value = value;
    public Dictionary<string, object?> ToDictionary() => Struct.Names.ToDictionary(name => name, name => Col(name).Value);
    public override string ToString() => "[" + string.Join(", ", Cells.Select(cell => PythonLiteral.Write(cell.Value))) + "]";
}

public abstract record DiffOp
{
    public abstract LiteralTuple ToTuple();
    internal abstract Action ApplyRaw(Table table);
    public string Code => (string)ToTuple().Items[0]!;
    public IReadOnlyList<object?> Args => ToTuple().Items.Skip(1).ToArray();
    public Action Apply(Table table) => ApplyRaw(table);
    public string Dumps() => PythonLiteral.Write(ToTuple()) + "\n";
}
public sealed record AddCol(string Name, ColumnType Type, int Index) : DiffOp
{
    public override LiteralTuple ToTuple() => new(["+c", Name, Values.TypeName(Type), Index]);
    internal override Action ApplyRaw(Table table) => table.InsertColumn(Name, Type, Index);
}
public sealed record DropCol(string Name) : DiffOp
{
    public override LiteralTuple ToTuple() => new(["-c", Name]); internal override Action ApplyRaw(Table table) => table.DropColumn(Name);
}
public sealed record SetColType(string Name, ColumnType Type) : DiffOp
{
    public override LiteralTuple ToTuple() => new(["=c", Name, Values.TypeName(Type)]); internal override Action ApplyRaw(Table table) => table.SetColumnType(Name, Type);
}
public sealed record RenameCol(string Name, string NewName) : DiffOp
{
    public override LiteralTuple ToTuple() => new([">c", Name, NewName]); internal override Action ApplyRaw(Table table) => table.RenameColumn(Name, NewName);
}
public sealed record AddRow(int Index, IReadOnlyDictionary<string, object?> Values) : DiffOp
{
    public override LiteralTuple ToTuple() => new(["+r", Index, Values.ToDictionary(p => p.Key, p => p.Value)]); internal override Action ApplyRaw(Table table) => table.InsertRow(Index, Values);
    public bool Equals(AddRow? other) => other is not null && Index == other.Index && SiQL.Values.Equal(ToTuple(), other.ToTuple());
    public override int GetHashCode() => Index;
}
public sealed record DropRow(int Index) : DiffOp
{
    public override LiteralTuple ToTuple() => new(["-r", Index]); internal override Action ApplyRaw(Table table) => table.DropRow(Index);
}
public sealed record SetCell(int Row, string Column, object? Value) : DiffOp
{
    public override LiteralTuple ToTuple() => new(["~", Row, Column, Value]); internal override Action ApplyRaw(Table table) => table.SetCell(Row, Column, Value);
    public bool Equals(SetCell? other) => other is not null && Row == other.Row && Column == other.Column && SiQL.Values.Equal(Value, other.Value);
    public override int GetHashCode() => HashCode.Combine(Row, Column);
}

public sealed class TableDiff : IEnumerable<DiffOp>, IEquatable<TableDiff>
{
    public List<DiffOp> Ops
    {
        get;
    }
    public TableDiff(IEnumerable<DiffOp>? ops = null) => Ops = ops?.ToList() ?? [];
    public int Count => Ops.Count;
    public Table Apply(Table table)
    {
        table.Commit(Ops);
        return table;
    }
    public string Dumps() => string.Concat(Ops.Select(op => op.Dumps()));
    public void Write(TextWriter writer) => writer.Write(Dumps());
    public static string CommitLine(IReadOnlyList<DiffOp> ops) => PythonLiteral.Write(ops.Count == 1 ? ops[0].ToTuple() : ops.Select(op => op.ToTuple()).ToList()) + "\n";
    public static TableDiff Loads(string text)
    {
        var result = new TableDiff();
        foreach (string line in text.Split('\n').Where(line => !string.IsNullOrWhiteSpace(line)))
        {
            object parsed = PythonLiteral.Read(line)!;
            foreach (LiteralTuple tuple in parsed is LiteralTuple single ? [single] : ((IEnumerable)parsed).Cast<LiteralTuple>())
            {
                object?[] a = tuple.Items;
                int I(int index) => checked((int)BigInteger.Parse(a[index]!.ToString()!));
                string S(int index) => (string)a[index]!;
                result.Ops.Add(S(0) switch
                {
                    "+c" => new AddCol(S(1), SiQL.Values.TypeFromName(S(2)), I(3)),
                    "-c" => new DropCol(S(1)),
                    "=c" => new SetColType(S(1), SiQL.Values.TypeFromName(S(2))),
                    ">c" => new RenameCol(S(1), S(2)),
                    "+r" => new AddRow(I(1), SiQL.Values.DictionaryEntries((IDictionary)a[2]!).ToDictionary(e => (string)e.Key, e => e.Value)),
                    "-r" => new DropRow(I(1)),
                    "~" => new SetCell(I(1), S(2), a[3]),
                    _ => throw new FormatException($"Unknown diff operation {S(0)}")
                });
            }
        }
        return result;
    }
    public static TableDiff Between(Table old, Table next)
    {
        var ops = new List<DiffOp>();
        foreach (string name in old.Struct.Names)
            if (!next.Struct.Names.Contains(name))
                ops.Add(new DropCol(name));
        for (int i = 0; i < next.Struct.Count; i++)
        {
            string name = next.Struct.Names[i];
            var type = next.Struct.Types[i];
            if (!old.Struct.Names.Contains(name))
                ops.Add(new AddCol(name, type, i));
            else if (old.Struct.Types[old.Struct.Col(name)] != type)
                ops.Add(new SetColType(name, type));
        }
        // Same earliest longest-contiguous matching blocks as difflib.SequenceMatcher(autojunk=False).
        object?[][] before = old.Rows.Select(row => next.Struct.Names.Select(name => old.Struct.Names.Contains(name) ? row.Col(name).Value : null).ToArray()).ToArray();
        object?[][] after = next.Rows.Select(row => next.Struct.Names.Select(name => row.Col(name).Value).ToArray()).ToArray();
        string[] a = before.Select(row => PythonLiteral.Write(new LiteralTuple(row))).ToArray(), b = after.Select(row => PythonLiteral.Write(new LiteralTuple(row))).ToArray();
        var blocks = new List<(int I, int J, int N)>();
        void Find(int alo, int ahi, int blo, int bhi)
        {
            int bestI = alo, bestJ = blo, best = 0;
            var previous = new int[bhi - blo + 1];
            for (int i = alo; i < ahi; i++)
            {
                var current = new int[previous.Length];
                for (int j = blo; j < bhi; j++)
                    if (a[i] == b[j])
                    {
                        int n = current[j - blo + 1] = previous[j - blo] + 1;
                        if (n > best)
                        {
                            best = n;
                            bestI = i - n + 1;
                            bestJ = j - n + 1;
                        }
                    }
                previous = current;
            }
            if (best == 0)
                return;
            if (alo < bestI && blo < bestJ)
                Find(alo, bestI, blo, bestJ);
            blocks.Add((bestI, bestJ, best));
            if (bestI + best < ahi && bestJ + best < bhi)
                Find(bestI + best, ahi, bestJ + best, bhi);
        }
        Find(0, a.Length, 0, b.Length);
        var merged = new List<(int I, int J, int N)>();
        foreach (var block in blocks)
        {
            if (merged.Count > 0 && merged[^1].I + merged[^1].N == block.I && merged[^1].J + merged[^1].N == block.J)
            {
                var previous = merged[^1];
                merged[^1] = (previous.I, previous.J, previous.N + block.N);
            }
            else
                merged.Add(block);
        }
        merged.Add((a.Length, b.Length, 0));
        int oi = 0, nj = 0, offset = 0;
        foreach (var block in merged)
        {
            int removed = block.I - oi, added = block.J - nj, pos = oi + offset, paired = Math.Min(removed, added);
            for (int k = 0; k < paired; k++)
                for (int c = 0; c < next.Struct.Count; c++)
                    if (!SiQL.Values.Equal(before[oi + k][c], after[nj + k][c]))
                        ops.Add(new SetCell(pos + k, next.Struct.Names[c], after[nj + k][c]));
            for (int k = paired; k < removed; k++)
                ops.Add(new DropRow(pos + paired));
            for (int k = paired; k < added; k++)
                ops.Add(new AddRow(pos + k, next.Rows[nj + k].ToDictionary()));
            offset += added - removed;
            oi = block.I + block.N;
            nj = block.J + block.N;
        }
        return new(ops);
    }
    public IEnumerator<DiffOp> GetEnumerator() => Ops.GetEnumerator();
    IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    public override string ToString() => Dumps();
    public bool Equals(TableDiff? other) => other is not null && Ops.SequenceEqual(other.Ops);
    public override bool Equals(object? other) => other is TableDiff diff && Equals(diff);
    public override int GetHashCode()
    {
        var hash = new HashCode();
        foreach (var op in Ops)
            hash.Add(op);
        return hash.ToHashCode();
    }
}

public sealed class TableFile
{
    public Table Table
    {
        get;
    }
    public TableFile(string? file = null) => Table = new Table(file: file);
    public TableDiff Diff(Table next) => TableDiff.Between(Table, next);
}
public sealed class ErrorHandler
{
}

public static class TextFormat
{
    public static string Box(IReadOnlyList<string> head, IEnumerable<IReadOnlyList<string>> rows)
    {
        var body = rows.ToList();
        var widths = head.Select((h, i) => Math.Max(Values.CodePoints(h).Count(), body.Select(row => Values.CodePoints(row[i]).Count()).DefaultIfEmpty(0).Max())).ToArray();
        string Rule(string left, string middle, string right) => left + string.Join(middle, widths.Select(w => new string('─', w + 2))) + right;
        string Line(IReadOnlyList<string> cells) => "│" + string.Join("│", cells.Select((cell, i) => " " + cell + new string(' ', widths[i] - Values.CodePoints(cell).Count()) + " ")) + "│";
        var lines = new List<string> { Rule("┌", "┬", "┐"), Line(head), Rule("├", "┼", "┤") };
        for (int i = 0; i < body.Count; i++)
        {
            if (i != 0)
                lines.Add(Rule("├", "┼", "┤"));
            lines.Add(Line(body[i]));
        }
        lines.Add(Rule("└", "┴", "┘"));
        return string.Join('\n', lines);
    }
}
