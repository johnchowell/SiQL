using System.Text;

namespace SiQL;

public sealed class Table
{
    readonly List<Row> rows = [];
    readonly Dictionary<string, List<Cell>> columns = [];
    readonly QueryCache cache;
    internal QueryCache QueryCache => cache;
    Dictionary<string, ColumnTree>? index;
    CompositeIndex? composite;
    Dictionary<Row, int>? positions;
    readonly List<int> deleted = [];
    bool lazyAdd, compressed, integer;
    internal string FileName;
    public string FilePath => FileName;
    public RowStruct Struct { get; private set; } = new();
    public IReadOnlyList<Row> Rows => rows.AsReadOnly();
    public IReadOnlyList<Row> Entries => Rows;
    public IReadOnlyDictionary<string, List<Cell>> Cols => columns;
    public IReadOnlyDictionary<string, ColumnTree>? Index => index;
    public CompositeIndex? CompositeIndex => composite;
    public bool LazyDelete
    {
        get; set;
    }
    public bool LazyAdd
    {
        get => lazyAdd; set
        {
            if (!value && index is not null)
                foreach (var node in index.Values)
                    node.FlushPending();
            lazyAdd = value;
        }
    }
    public long CacheMaxBytes
    {
        get => cache.MaxBytes; set => cache.MaxBytes = value;
    }
    public bool CacheTraversedNodes
    {
        get => cache.TraversedNodes; set => cache.TraversedNodes = value;
    }
    public CacheInfo CacheInfo => cache.Info;
    public bool CompressedSearch
    {
        get => compressed; set
        {
            compressed = value;
            ConfigureComposite();
        }
    }
    public bool IntegerSearch
    {
        get => integer; set
        {
            integer = value;
            ConfigureComposite();
        }
    }
    public IReadOnlyDictionary<string, object?> CompressedInfo => composite is { Integer: false } ? composite.Info : new Dictionary<string, object?> { ["enabled"] = compressed, ["active"] = false };
    public IReadOnlyDictionary<string, object?> IntegerInfo => composite is { Integer: true } ? composite.Info : new Dictionary<string, object?> { ["enabled"] = integer, ["active"] = false };
    public bool Tree
    {
        get => index is not null;
        set
        {
            if (value && index is null)
            {
                index = [];
                foreach (string name in Struct.Names)
                    IndexColumn(name);
                ConfigureComposite();
            }
            else if (!value && index is not null)
            {
                ClearCache();
                foreach (var node in index.Values)
                    node.Sweep();
                foreach (var cell in columns.Values.SelectMany(c => c))
                    cell.Tree = null;
                index = null;
                composite?.Close();
                composite = null;
            }
        }
    }
    public Table(IEnumerable<Row>? initialRows = null, string? file = null, bool tree = true, bool lazyDelete = false,
        long cacheMaxBytes = 100_000_000, bool cacheTraversedNodes = false, bool compressedSearch = false,
        bool lazyAdd = false, bool integerSearch = false)
    {
        FileName = file ?? Guid.NewGuid().ToString("N") + ".siql";
        cache = new(cacheMaxBytes, cacheTraversedNodes);
        LazyDelete = lazyDelete;
        this.lazyAdd = lazyAdd;
        if (File.Exists(FileName))
            Load();
        if (initialRows?.ToList() is { Count: > 0 } initial)
        {
            if (rows.Count != 0 || Struct.Count != 0)
                throw new ArgumentException("Cannot pass initial rows for an existing table");
            var source = initial[0].Struct;
            var ops = source.ColumnNames.Select((name, i) => (DiffOp)new AddCol(name, source.Columns[i], i)).ToList();
            ops.AddRange(initial.Select((row, i) => new AddRow(i, row.ToDictionary())));
            string line = TableDiff.CommitLine(ops);
            Struct = source;
            rows.AddRange(initial);
            foreach (string name in Struct.Names)
                columns[name] = rows.Select(row => row.Col(name)).ToList();
            File.AppendAllText(FileName, line, new UTF8Encoding(false));
        }
        if (!File.Exists(FileName))
            File.WriteAllText(FileName, "", new UTF8Encoding(false));
        Tree = tree;
        compressed = compressedSearch;
        integer = integerSearch;
        ConfigureComposite();
    }
    void ConfigureComposite()
    {
        if (index is null || !compressed && !integer)
        {
            composite?.Close();
            composite = null;
            return;
        }
        if (composite is not null && composite.Integer == integer)
            return;
        composite?.Close();
        composite = integer ? new IntegerIndex(this) : new CompressedIndex(this);
    }
    void Changed(Row row, Cell cell) => composite?.Changed(row);
    void IndexColumn(string name)
    {
        var node = index![name] = new(cache, onChange: Changed);
        for (int i = 0; i < rows.Count; i++)
            node.Add(rows[i], columns[name][i]);
    }
    void SchemaChanged() => composite?.SchemaChanged();
    void ForgetPositions()
    {
        positions = null;
        deleted.Clear();
    }
    void RowAdded(Row row, int position)
    {
        composite?.Add(row);
        if (positions is not null && position == rows.Count - 1)
            positions[row] = position + deleted.Count;
        else
            ForgetPositions();
        if (index is not null)
            foreach (var (name, node) in index)
            {
                if (lazyAdd)
                    node.QueueAdd(row, row.Col(name));
                else
                    node.Add(row, row.Col(name));
            }
    }
    void RowRemoved(Row row, int position)
    {
        composite?.Remove(row);
        if (positions is not null)
        {
            // Position offsets are independent of eager/lazy tree cleanup.
            if (deleted.Count < rows.Count / 4 + 64)
            {
                int p = positions[row];
                positions.Remove(row);
                int i = deleted.BinarySearch(p);
                deleted.Insert(i < 0 ? ~i : i, p);
            }
            else if (position == rows.Count && deleted.Count == 0)
                positions.Remove(row);
            else
                ForgetPositions();
        }
        if (index is not null)
            foreach (var (name, node) in index)
            {
                if (LazyDelete)
                    node.Bury(row.Col(name));
                else
                    node.Remove(row.Col(name));
            }
    }
    void Load()
    {
        byte[] data = File.ReadAllBytes(FileName);
        int end = Array.LastIndexOf(data, (byte)'\n') + 1;
        if (end < data.Length)
        {
            using var stream = new FileStream(FileName, FileMode.Open, FileAccess.Write);
            stream.SetLength(end);
        }
        foreach (var op in TableDiff.Loads(Encoding.UTF8.GetString(data, 0, end)))
            op.ApplyRaw(this);
    }
    internal void Commit(IReadOnlyList<DiffOp> ops)
    {
        if (ops.Count == 0)
            return;
        string line = TableDiff.CommitLine(ops);
        var undo = new List<Action>();
        try
        {
            foreach (var op in ops)
                undo.Add(op.ApplyRaw(this));
        }
        catch { for (int i = undo.Count - 1; i >= 0; i--) undo[i](); throw; }
        File.AppendAllText(FileName, line, new UTF8Encoding(false));
    }
    public void Save()
    { /* Commits write immediately, as in the Python implementation. */
    }
    public void Compact()
    {
        var ops = Struct.Names.Select((name, i) => (DiffOp)new AddCol(name, Struct.Types[i], i)).ToList();
        ops.AddRange(rows.Select((row, i) => new AddRow(i, row.ToDictionary())));
        string temporary = FileName + ".tmp";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write))
        {
            byte[] bytes = Encoding.UTF8.GetBytes(ops.Count == 0 ? "" : TableDiff.CommitLine(ops));
            stream.Write(bytes);
            stream.Flush(true);
        }
        File.Move(temporary, FileName, true);
    }
    public void ClearCache() => cache.Clear();
    public void Purge()
    {
        if (index is not null)
            foreach (var node in index.Values)
                node.Sweep();
        ForgetPositions();
    }
    internal Action InsertColumn(string name, ColumnType type, int position)
    {
        if (columns.ContainsKey(name))
            throw new ArgumentException($"Column already exists: {name}");
        position = Math.Clamp(position < 0 ? Struct.Count + position : position, 0, Struct.Count);
        foreach (var row in rows)
            row.Fill();
        Struct.Names.Insert(position, name);
        Struct.Types.Insert(position, type);
        foreach (var row in rows)
            row.Cells.Insert(position, new());
        columns[name] = rows.Select(row => row.Col(name)).ToList();
        SchemaChanged();
        if (index is not null)
            IndexColumn(name);
        return () => DropColumn(name);
    }
    internal Action DropColumn(string name)
    {
        int i = Struct.Col(name);
        var type = Struct.Types[i];
        foreach (var row in rows)
            row.Fill();
        Struct.Names.RemoveAt(i);
        Struct.Types.RemoveAt(i);
        var removed = rows.Select(row => { var c = row.Cells[i]; row.Cells.RemoveAt(i); return c; }).ToArray();
        var cells = columns[name];
        columns.Remove(name);
        SchemaChanged();
        if (index is not null)
        {
            var node = index[name];
            node.ClearCache();
            node.Sweep();
            index.Remove(name);
            foreach (var cell in cells)
                cell.Tree = null;
        }
        return () => { Struct.Names.Insert(i, name); Struct.Types.Insert(i, type); for (int j = 0; j < rows.Count; j++) rows[j].Cells.Insert(i, removed[j]); columns[name] = cells; SchemaChanged(); if (index is not null) IndexColumn(name); };
    }
    internal Action RenameColumn(string name, string next)
    {
        int i = Struct.Col(name);
        if (columns.ContainsKey(next))
            throw new ArgumentException($"Column already exists: {next}");
        Struct.Names[i] = next;
        columns[next] = columns[name];
        columns.Remove(name);
        if (index is not null)
        {
            index[next] = index[name];
            index.Remove(name);
        }
        SchemaChanged();
        return () => RenameColumn(next, name);
    }
    internal Action SetColumnType(string name, ColumnType type)
    {
        int i = Struct.Col(name);
        var old = Struct.Types[i];
        Struct.Types[i] = type;
        return () => Struct.Types[i] = old;
    }
    internal Action InsertRow(int position, IReadOnlyDictionary<string, object?> values)
    {
        var row = new Row(Struct);
        foreach (var (name, value) in values)
            row.Col(name).Value = value;
        position = Math.Clamp(position < 0 ? rows.Count + position : position, 0, rows.Count);
        rows.Insert(position, row);
        foreach (string name in Struct.Names)
            columns[name].Insert(position, row.Col(name));
        RowAdded(row, position);
        return () => DropRow(position);
    }
    internal Action DropRow(int position)
    {
        if (position < 0)
            position += rows.Count;
        var row = rows[position];
        rows.RemoveAt(position);
        foreach (var cells in columns.Values)
            cells.RemoveAt(position);
        RowRemoved(row, position);
        return () => { rows.Insert(position, row); foreach (string name in Struct.Names) columns[name].Insert(position, row.Col(name)); RowAdded(row, position); };
    }
    internal Action SetCell(int row, string name, object? value)
    {
        if (row < 0)
            row += rows.Count;
        var cell = rows[row].Col(name);
        var old = cell.Value;
        cell.Value = value;
        return () => cell.Value = old;
    }
    public IEnumerable<Cell> Column(string name) => columns[name];
    public IEnumerable<Cell> Column(int number) => Column(Struct.Names[number < 0 ? Struct.Count + number : number]);
    internal List<int> Positions(IEnumerable<Row> matches)
    {
        if (positions is null)
        {
            positions = rows.Select((row, i) => (row, i)).ToDictionary(p => p.row, p => p.i);
            deleted.Clear();
        }
        var found = matches.Select(row => positions[row]).Order().ToList();
        if (deleted.Count != 0)
            for (int i = 0; i < found.Count; i++)
            {
                int offset = deleted.BinarySearch(found[i]);
                found[i] -= offset < 0 ? ~offset : offset;
            }
        return found;
    }
    public List<int> Find(string column, object? value) => index is null ? columns[column].Select((cell, i) => (cell, i)).Where(p => Values.Equal(value, p.cell.Value)).Select(p => p.i).ToList() : Positions(index[column].Find(value));
    public List<int> Find(int column, object? value) => Find(Struct.Names[column < 0 ? Struct.Count + column : column], value);
    public List<int> FindRange(string column, object? low = null, object? high = null, bool lowInclusive = true, bool highInclusive = true)
    {
        // Rows whose value lies between two bounds (null: unbounded), compared as the <, <=, >, >= scans compare them.
        if (IndexRange(column, low, high, lowInclusive, highInclusive) is { } found)
            return found;
        var cells = columns[column];
        return Enumerable.Range(0, cells.Count).Where(i =>
            (low is null || Values.Match(cells[i].Value, lowInclusive ? ">=" : ">", low)) &&
            (high is null || Values.Match(cells[i].Value, highInclusive ? "<=" : "<", high))).ToList();
    }
    public List<int> FindRange(int column, object? low = null, object? high = null, bool lowInclusive = true, bool highInclusive = true) =>
        FindRange(Struct.Names[column < 0 ? Struct.Count + column : column], low, high, lowInclusive, highInclusive);
    internal List<int>? IndexRange(string column, object? low, object? high, bool lowInclusive, bool highInclusive, int? limit = null)
    {
        if (index is null)
            return null;
        // Past about a quarter of the rows, one pass over the column costs less than resolving positions.
        int cap = rows.Count / 4 + 64;
        return index[column].FindRange(low, high, lowInclusive, highInclusive, Math.Min(limit ?? cap, cap)) is { } found ? Positions(found) : null;
    }
    public List<List<int>> FindBatch(int column, IEnumerable<object?> values, string order = "tree", bool chained = true) => FindBatch(Struct.Names[column < 0 ? Struct.Count + column : column], values, order, chained);
    public List<List<int>> FindBatch(string column, IEnumerable<object?> values, string order = "tree", bool chained = true)
    {
        if (order is not "tree" and not "input")
            throw new ArgumentException("order must be tree or input");
        return index is null ? values.Select(value => Find(column, value)).ToList() : index[column].FindBatch(values, order, chained).Select(Positions).ToList();
    }
    public List<int> FindWhere(IReadOnlyDictionary<string, object?> criteria)
    {
        foreach (string column in criteria.Keys)
            if (!columns.ContainsKey(column))
                throw new KeyNotFoundException(column);
        if (criteria.Count == 0)
            return Enumerable.Range(0, rows.Count).ToList();
        if (criteria.Count == 1)
        {
            var p = criteria.First();
            return Find(p.Key, p.Value);
        }
        if (composite is not null)
            return Positions(composite.Find(criteria));
        var items = index is null ? criteria.ToArray() : criteria.OrderBy(p => index[p.Key].Estimate(p.Value)).ToArray();
        var first = items[0];
        return Find(first.Key, first.Value).Where(i => items.Skip(1).All(p => Values.Equal(p.Value, rows[i].Col(p.Key).Value))).ToList();
    }
    public IEnumerable<Row> Select(object? value, string? column = null)
    {
        if (column is not null)
        {
            foreach (int i in Find(column, value))
                yield return rows[i];
            yield break;
        }
        ColumnType kind = value switch
        {
            null => ColumnType.NoneType,
            string => ColumnType.Str,
            bool => ColumnType.Bool,
            double or float => ColumnType.Float,
            byte[] => ColumnType.Bytes,
            System.Collections.IDictionary => ColumnType.Dict,
            System.Collections.IList => ColumnType.List,
            LiteralTuple => ColumnType.Tuple,
            HashSet<object?> => ColumnType.Set,
            System.Numerics.Complex => ColumnType.Complex,
            _ when Values.Numeric(value) => ColumnType.Int,
            _ => ColumnType.Any
        };
        foreach (string name in Struct.Names.Where((_, i) => Struct.Types[i] == kind))
            foreach (int i in Find(name, value))
                yield return rows[i];
    }
    public void AddCols(IReadOnlyDictionary<string, ColumnType> additions) => Commit(additions.Select((p, i) => (DiffOp)new AddCol(p.Key, p.Value, Struct.Count + i)).ToArray());
    public void Add(IReadOnlyDictionary<string, object?> values) => Commit([new AddRow(rows.Count, values.Where(p => columns.ContainsKey(p.Key)).ToDictionary(p => p.Key, p => p.Value))]);
    public void EditByName(string column, object? where, object? change) => Commit(Find(column, where).Select(i => (DiffOp)new SetCell(i, column, change)).ToArray());
    public override string ToString() => Struct.Count == 0 ? "" : TextFormat.Box(Struct.Names.Select((name, i) => $"{name}: {Values.TypeName(Struct.Types[i])}").ToArray(), rows.Select(row => (IReadOnlyList<string>)row.Cells.Select(cell => cell.ToString()).ToArray()));
}
