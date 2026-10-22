using System.Collections;
using System.Globalization;
using System.Numerics;
using System.Text;

namespace SiQL;

public sealed record Result(IReadOnlyList<string>? Columns = null, IReadOnlyList<IReadOnlyList<object?>>? Rows = null, int RowCount = 0, string Message = "")
{
    public IReadOnlyList<string> Columns { get; init; } = Columns ?? [];
    public IReadOnlyList<IReadOnlyList<object?>> Rows { get; init; } = Rows ?? [];
    public Dictionary<string, object?> ToDictionary() => new() { ["columns"] = Columns, ["rows"] = Rows, ["rowcount"] = RowCount, ["message"] = Message };
    public override string ToString() => Columns.Count == 0 ? Message : TextFormat.Box(Columns, Rows.Select(row => (IReadOnlyList<string>)row.Select(value => value is string s ? s : PythonLiteral.Write(value)).ToArray()));
}

public static class Command
{
    public const int SELECT = 0, WHERE = 1, FROM = 2, AND = 3, OR = 4, ORDER = 5, BY = 6;
    public static Func<object?, object?, bool> StrToOp(string op)
    {
        if (!new[] { "=", "==", "!=", "<>", "<", ">", "<=", ">=" }.Contains(op))
            throw new ArgumentException($"Unknown operator: {op}");
        return (a, b) => Values.Match(a, op, b);
    }
    public static int? FromStr(string value) => value.ToLowerInvariant() switch { "select" => 0, "where" => 1, "equals" => 2, "from" => 3, "and" => 4, "or" => 5, "if" => 6, _ => null };
}

public sealed class Interpreter
{
    public Database Database
    {
        get; private set;
    }
    public bool FileAccess
    {
        get;
    }
    public Interpreter(string database = ".", bool fileAccess = true) : this(new Database(database), fileAccess) { }
    public Interpreter(Database database, bool fileAccess = true)
    {
        Database = database;
        FileAccess = fileAccess;
    }
    static string Plural(int n, string word) => $"{n} {word}" + (n == 1 ? "" : "s");
    public Result Execute(string query)
    {
        var statements = Parser.Parse(query);
        if (statements.Count != 1)
            throw new QueryError($"execute() runs one statement, got {statements.Count}; use executescript()");
        return Run(statements[0]);
    }
    public IReadOnlyList<Result> ExecuteScript(string query) => Parser.Parse(query).Select(Run).ToArray();
    public Result Run(Statement statement)
    {
        try
        {
            return RunCore(statement);
        }
        catch (QueryError) { throw; }
        catch (Exception e) when (e is ArgumentException or KeyNotFoundException or IndexOutOfRangeException or FormatException or IOException or DecoderFallbackException) { throw new QueryError(e.Message); }
    }
    Table Get(string name) => Database.Table(name);
    static void Check(Table table, IEnumerable<string> columns)
    {
        foreach (string name in columns)
            if (!table.Struct.Names.Contains(name))
                throw new QueryError($"No such column: {name}");
    }
    void CheckFiles(string statement)
    {
        if (!FileAccess)
            throw new QueryError($"{statement} is disabled");
    }
    static (string Column, object? Value)? Equality(Expr expression) => expression switch { Compare(Column c, "=" or "==", Literal l) => (c.Name, l.Value), Compare(Literal l, "=" or "==", Column c) => (c.Name, l.Value), _ => null };
    // `column op literal` in either order, with the operator as seen from the column.
    static (string Column, string Op, object? Value)? Comparison(Expr expression) => expression switch
    {
        Compare(Column c, var op, Literal l) => (c.Name, op, l.Value),
        Compare(Literal l, var op, Column c) => (c.Name, op switch { "<" => ">", "<=" => ">=", ">" => "<", ">=" => "<=", _ => op }, l.Value),
        _ => null
    };
    static List<int>? IndexRange(Table table, Expr expression) =>
        table.Tree && Comparison(expression) is { Op: "<" or "<=" or ">" or ">=", Value: { } value } r
            ? table.IndexRange(r.Column, r.Op[0] == '>' ? value : null, r.Op[0] == '<' ? value : null, r.Op == ">=", r.Op == "<=")
            : null;
    static List<int> MatchAnd(Table table, And a)
    {
        // The most selective item the tree can read gives the candidates; the other items are only checked on those.
        List<int>? positions = null;
        var used = new List<Expr>();
        if (table.Tree)
        {
            (int Count, Expr Item)? best = null;
            var ranges = new Dictionary<string, (string Op, object Value, Expr Item)?[]>();
            foreach (var item in a.Items)
            {
                int? count = null;
                if (Equality(item) is { } eq)
                    count = table.Index![eq.Column].Estimate(eq.Value);
                else if (item is In { Operand: Column col, Negate: false } ins && ins.Items.All(x => x is Literal))
                    count = ins.Items.Sum(x => table.Index![col.Name].Estimate(((Literal)x).Value));
                else if (Comparison(item) is { Op: "<" or "<=" or ">" or ">=", Value: { } value } r)
                {
                    // The first lower and upper bound of one kind on a column make one read.
                    if (!ranges.TryGetValue(r.Column, out var pair))
                        ranges[r.Column] = pair = new (string, object, Expr)?[2];
                    int side = r.Op[0] == '<' ? 1 : 0;
                    var other = pair[1 - side];
                    if (pair[side] is null && (other is null || (other.Value.Value is string) == (value is string)))
                        pair[side] = (r.Op, value, item);
                }
                if (count is { } c && (best is null || c < best.Value.Count))
                    best = (c, item);
            }
            if (best is { } b)
            {
                positions = Match(table, b.Item).Order().ToList();
                used.Add(b.Item);
                if (positions.Count == 0)
                    return positions;
            }
            foreach (var (column, pair) in ranges)
            {
                // A range read stops once it holds as many rows as the best candidates so far.
                var (low, high) = (pair[0], pair[1]);
                if (table.IndexRange(column, low?.Value, high?.Value, low?.Op == ">=", high?.Op == "<=",
                        positions is null ? null : positions.Count - 1) is { } found)
                {
                    positions = found;
                    used = pair.Where(p => p is not null).Select(p => p!.Value.Item).ToList();
                    if (positions.Count == 0)
                        return positions;
                }
            }
        }
        if (positions is null)
        {
            positions = Match(table, a.Items[0]).Order().ToList();
            used.Add(a.Items[0]);
        }
        foreach (var item in a.Items)
        {
            if (positions.Count == 0)
                break;
            if (!used.Any(u => ReferenceEquals(u, item)))
                positions = positions.Where(i => Values.Truth(item.Eval(table.Rows[i].ToDictionary()))).ToList();
        }
        return positions;
    }
    static List<int>? Leading(Table table, Select select)
    {
        // The first OFFSET + LIMIT rows of a single-column ORDER BY without WHERE, read in order from the tree's lanes
        // instead of sorting every row.
        if (select.Where is not null || select.Limit is not { } limit || select.Order is not { Count: 1 } order || !table.Tree)
            return null;
        var (column, descending) = order[0];
        int keep = select.Offset + limit;
        if (table.Index![column].First(keep, descending) is not { } found)
            return null;
        var ordered = found.SelectMany(p => table.Positions(p.Rows)).ToList();
        var nulls = !descending || ordered.Count < keep ? table.Find(column, null) : [];
        return (descending ? ordered.Concat(nulls) : nulls.Concat(ordered)).Take(keep).ToList();
    }
    static HashSet<int> Match(Table table, Expr? where)
    {
        if (where is null)
            return Enumerable.Range(0, table.Rows.Count).ToHashSet();
        Check(table, where.Columns);
        if (table.Tree && Equality(where) is { } eq)
            return table.Find(eq.Column, eq.Value).ToHashSet();
        if (IndexRange(table, where) is { } range)
            return range.ToHashSet();
        if (where is And a)
        {
            if (table.Tree && (table.CompressedSearch || table.IntegerSearch))
            {
                var criteria = new Dictionary<string, object?>();
                bool simple = true;
                foreach (var item in a.Items)
                {
                    if (Equality(item) is not { } p || !criteria.TryAdd(p.Column, p.Value))
                    {
                        simple = false;
                        break;
                    }
                }
                if (simple)
                    return table.FindWhere(criteria).ToHashSet();
            }
            return MatchAnd(table, a).ToHashSet();
        }
        if (where is Or o)
        {
            var result = new HashSet<int>();
            foreach (var item in o.Items)
                result.UnionWith(Match(table, item));
            return result;
        }
        if (where is Not n)
        {
            var result = Enumerable.Range(0, table.Rows.Count).ToHashSet();
            result.ExceptWith(Match(table, n.Item));
            return result;
        }
        if (table.Tree && where is In { Operand: Column col, Negate: false } ins && ins.Items.All(item => item is Literal))
            return table.FindBatch(col.Name, ins.Items.Cast<Literal>().Select(l => l.Value)).SelectMany(x => x).ToHashSet();
        return Enumerable.Range(0, table.Rows.Count).Where(i => Values.Truth(where.Eval(table.Rows[i].ToDictionary()))).ToHashSet();
    }
    Result RunCore(Statement s)
    {
        switch (s)
        {
            case CreateTable c:
                if (c.IfNotExists && Database.Contains(c.Table))
                    return new(Message: $"Table {c.Table} already exists");
                Database.Create(c.Table, c.Columns);
                return new(Message: $"Created table {c.Table}");
            case DropTable d:
                if (d.IfExists && !Database.Contains(d.Table))
                    return new(Message: $"Table {d.Table} doesn't exist");
                Database.Drop(d.Table);
                return new(Message: $"Dropped table {d.Table}");
            case AddColumn a:
                {
                    var t = Get(a.Table);
                    new TableDiff([new AddCol(a.Column, a.Type, t.Struct.Count)]).Apply(t);
                    return new(Message: $"Added column {a.Column}");
                }
            case DropColumn d:
                {
                    var t = Get(d.Table);
                    Check(t, [d.Column]);
                    new TableDiff([new DropCol(d.Column)]).Apply(t);
                    return new(Message: $"Dropped column {d.Column}");
                }
            case AlterColumn a:
                {
                    var t = Get(a.Table);
                    Check(t, [a.Column]);
                    foreach (var cell in t.Column(a.Column))
                        Values.Coerce(cell.Value, a.Type, a.Column);
                    new TableDiff([new SetColType(a.Column, a.Type)]).Apply(t);
                    return new(Message: $"Changed column {a.Column} to {Values.TypeName(a.Type)}");
                }
            case RenameColumn r:
                {
                    var t = Get(r.Table);
                    Check(t, [r.Column]);
                    new TableDiff([new RenameCol(r.Column, r.NewName)]).Apply(t);
                    return new(Message: $"Renamed column {r.Column} to {r.NewName}");
                }
            case Insert i:
                {
                    var t = Get(i.Table);
                    var cols = i.Columns ?? t.Struct.ColumnNames;
                    Check(t, cols);
                    if (cols.Distinct().Count() != cols.Count)
                        throw new QueryError("A column is listed more than once");
                    var ops = new List<DiffOp>();
                    foreach (var row in i.Rows)
                    {
                        if (row.Count != cols.Count)
                            throw new QueryError($"Got {row.Count} values for {cols.Count} columns");
                        var record = cols.Select((c, j) => (c, value: Values.Coerce(row[j], t.Struct.Types[t.Struct.Col(c)], c))).ToDictionary(p => p.c, p => p.value);
                        ops.Add(new AddRow(t.Rows.Count + ops.Count, record));
                    }
                    new TableDiff(ops).Apply(t);
                    return new(RowCount: ops.Count, Message: $"Inserted {Plural(ops.Count, "row")}");
                }
            case Select select:
                {
                    var t = Get(select.Table);
                    var cols = select.Columns ?? t.Struct.ColumnNames;
                    var order = select.Order ?? [];
                    Check(t, cols.Concat(order.Select(p => p.Column)));
                    var indices = Leading(t, select);
                    if (indices is null)
                    {
                        indices = Match(t, select.Where).Order().ToList();
                        foreach (var (col, desc) in order.Reverse())
                        {
                            var nulls = indices.Where(i => t.Rows[i].Col(col).Value is null).ToList();
                            var rest = indices.Where(i => t.Rows[i].Col(col).Value is not null);
                            try
                            {
                                var sorted = (desc ? rest.OrderByDescending(i => t.Rows[i].Col(col).Value, Comparer<object?>.Create(Values.Compare)) : rest.OrderBy(i => t.Rows[i].Col(col).Value, Comparer<object?>.Create(Values.Compare))).ToList();
                                indices = desc ? sorted.Concat(nulls).ToList() : nulls.Concat(sorted).ToList();
                            }
                            catch (InvalidOperationException) { throw new QueryError($"Can't order by {col}: it holds values of different types"); }
                        }
                    }
                    indices = indices.Skip(select.Offset).Take(select.Limit ?? int.MaxValue).ToList();
                    if (select.Count)
                        return new(["count"], new IReadOnlyList<object?>[] { new object?[] { indices.Count } }, 1, Plural(indices.Count, "row"));
                    return new(cols, indices.Select(i => (IReadOnlyList<object?>)cols.Select(c => t.Rows[i].Col(c).Value).ToArray()).ToArray(), indices.Count, Plural(indices.Count, "row"));
                }
            case Update u:
                {
                    var t = Get(u.Table);
                    Check(t, u.Values.Keys);
                    var vals = u.Values.ToDictionary(p => p.Key, p => Values.Coerce(p.Value, t.Struct.Types[t.Struct.Col(p.Key)], p.Key));
                    var matched = Match(t, u.Where).Order().ToArray();
                    new TableDiff(matched.SelectMany(i => vals.Select(p => (DiffOp)new SetCell(i, p.Key, p.Value)))).Apply(t);
                    return new(RowCount: matched.Length, Message: $"Updated {Plural(matched.Length, "row")}");
                }
            case Delete d:
                {
                    var t = Get(d.Table);
                    var matched = Match(t, d.Where).OrderDescending().ToArray();
                    new TableDiff(matched.Select(i => (DiffOp)new DropRow(i))).Apply(t);
                    return new(RowCount: matched.Length, Message: $"Deleted {Plural(matched.Length, "row")}");
                }
            case ShowTables:
                {
                    var tables = Database.Tables();
                    return new(["table"], tables.Select(n => (IReadOnlyList<object?>)new object?[] { n }).ToArray(), tables.Count, Plural(tables.Count, "table"));
                }
            case Describe d:
                {
                    var t = Get(d.Table);
                    return new(["column", "type"], t.Struct.Names.Select((name, i) => (IReadOnlyList<object?>)new object?[] { name, Values.TypeName(t.Struct.Types[i]) }).ToArray(), t.Struct.Count, Plural(t.Struct.Count, "column"));
                }
            case RenameTable r:
                Database.Rename(r.Table, r.NewName);
                return new(Message: $"Renamed table {r.Table} to {r.NewName}");
            case CopyTable c:
                Database.Copy(c.Table, c.NewName);
                return new(Message: $"Copied table {c.Table} to {c.NewName}");
            case Truncate tr:
                {
                    var t = Get(tr.Table);
                    int count = t.Rows.Count;
                    new TableDiff(Enumerable.Range(0, count).Reverse().Select(i => (DiffOp)new DropRow(i))).Apply(t);
                    return new(RowCount: count, Message: $"Deleted {Plural(count, "row")}");
                }
            case Compact c:
                {
                    var names = c.Table is null ? Database.Tables() : new[] { c.Table };
                    var results = new List<IReadOnlyList<object?>>();
                    foreach (string name in names)
                    {
                        var t = Get(name);
                        long before = new FileInfo(t.FilePath).Length;
                        t.Compact();
                        results.Add(new object?[] { name, before, new FileInfo(t.FilePath).Length });
                    }
                    return new(["table", "bytes_before", "bytes_after"], results, results.Count, $"Compacted {Plural(results.Count, "table")}");
                }
            case Use u:
                CheckFiles("USE");
                Database = new(u.Path);
                return new(Message: $"Using {Database.Path}");
            case ShowFiles:
                {
                    var names = Database.Tables();
                    return new(["table", "file", "bytes", "rows"], names.Select(name => (IReadOnlyList<object?>)new object?[] { name, Database.File(name), new FileInfo(Database.File(name)).Length, Get(name).Rows.Count }).ToArray(), names.Count, Plural(names.Count, "table"));
                }
            case Import i:
                return ImportCsv(i);
            case Export e:
                return ExportCsv(e);
            default:
                throw new QueryError("Unsupported statement");
        }
    }
    static object? FromText(string text, ColumnType type)
    {
        if (text.Length == 0)
            return null;
        return type switch
        {
            ColumnType.Str => text,
            ColumnType.Int => BigInteger.TryParse(text, System.Globalization.NumberStyles.Integer, CultureInfo.InvariantCulture, out var n) ? n >= long.MinValue && n <= long.MaxValue ? (object)(long)n : n : throw new FormatException(),
            ColumnType.Float => double.Parse(text, CultureInfo.InvariantCulture),
            ColumnType.Bool => text.ToLowerInvariant() switch { "true" or "1" => true, "false" or "0" => false, _ => throw new FormatException() },
            ColumnType.Any => AnyText(text),
            _ => Values.Coerce(PythonLiteral.Read(text), type, "")
        };
    }
    static object? AnyText(string text)
    {
        try
        {
            return PythonLiteral.Read(text);
        }
        catch (FormatException) { return text; }
    }
    static ColumnType Infer(IEnumerable<string> texts)
    {
        var all = texts.Where(s => s != "").ToArray();
        if (all.Length == 0)
            return ColumnType.Str;
        foreach (var type in new[] { ColumnType.Int, ColumnType.Float, ColumnType.Bool })
            try
            {
                foreach (var s in all)
                    FromText(s, type);
                return type;
            }
            catch (Exception e) when (e is FormatException or QueryError) { }
        return ColumnType.Str;
    }
    Result ImportCsv(Import import)
    {
        CheckFiles("IMPORT");
        var lines = Csv.Read(File.ReadAllText(import.Path, new UTF8Encoding(false, true)));
        if (lines.Count == 0 || lines[0].Any(s => s == "") || lines[0].Distinct().Count() != lines[0].Count)
            throw new QueryError($"{import.Path} needs a header row of unique column names");
        var names = lines[0];
        var data = lines.Skip(1).ToArray();
        foreach (var line in data)
            if (line.Count != names.Count)
                throw new QueryError($"{import.Path} line has {line.Count} fields, expected {names.Count}");
        bool exists = Database.Contains(import.Table);
        Table? table = exists ? Get(import.Table) : null;
        if (table is not null)
            Check(table, names);
        var types = names.Select((name, i) => (name, type: table is null ? Infer(data.Select(row => row[i])) : table.Struct.Types[table.Struct.Col(name)])).ToDictionary(p => p.name, p => p.type);
        var records = data.Select(line => names.Select((name, i) => (name, value: FromText(line[i], types[name]))).ToDictionary(p => p.name, p => p.value)).ToArray();
        table ??= Database.Create(import.Table, types);
        new TableDiff(records.Select((row, i) => (DiffOp)new AddRow(table.Rows.Count + i, row))).Apply(table);
        return new(RowCount: records.Length, Message: $"Imported {Plural(records.Length, "row")} into {import.Table}");
    }
    Result ExportCsv(Export export)
    {
        CheckFiles("EXPORT");
        var t = Get(export.Table);
        var lines = new List<IReadOnlyList<string>> { t.Struct.ColumnNames };
        lines.AddRange(t.Rows.Select(row => (IReadOnlyList<string>)t.Struct.Names.Select(name => row.Col(name).Value switch { null => "", bool b => b ? "true" : "false", string s => s, var v => PythonLiteral.Write(v) }).ToArray()));
        File.WriteAllText(export.Path, Csv.Write(lines), new UTF8Encoding(false));
        return new(RowCount: t.Rows.Count, Message: $"Exported {Plural(t.Rows.Count, "row")} to {export.Path}");
    }
}

public static class Csv
{
    public static IReadOnlyList<IReadOnlyList<string>> Read(string text)
    {
        text = text.TrimStart('\ufeff');
        var rows = new List<IReadOnlyList<string>>();
        var row = new List<string>();
        var field = new StringBuilder();
        bool quote = false, active = false;
        for (int i = 0; i < text.Length; i++)
        {
            char c = text[i];
            bool started = active;
            active = true;
            if (quote)
            {
                if (c == '"')
                {
                    if (i + 1 < text.Length && text[i + 1] == '"')
                    {
                        field.Append('"');
                        i++;
                    }
                    else
                        quote = false;
                }
                else
                    field.Append(c);
            }
            else if (c == '"' && field.Length == 0)
                quote = true;
            else if (c == ',')
            {
                row.Add(field.ToString());
                field.Clear();
            }
            else if (c is '\r' or '\n')
            {
                if (c == '\r' && i + 1 < text.Length && text[i + 1] == '\n')
                    i++;
                if (started || row.Count != 0 || field.Length != 0)
                    row.Add(field.ToString());
                field.Clear();
                rows.Add(row);
                row = [];
                active = false;
            }
            else
                field.Append(c);
        }
        if (quote)
            throw new QueryError("Unterminated CSV field");
        if (active)
        {
            row.Add(field.ToString());
            rows.Add(row);
        }
        return rows;
    }
    public static string Write(IEnumerable<IReadOnlyList<string>> rows) => string.Concat(rows.Select(row => string.Join(',', row.Select(s => s.IndexOfAny([',', '"', '\r', '\n']) >= 0 ? "\"" + s.Replace("\"", "\"\"") + "\"" : s)) + "\r\n"));
}
