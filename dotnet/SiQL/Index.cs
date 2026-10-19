namespace SiQL;

public sealed record CacheInfo(long MaxBytes, long Bytes, int Entries, long Hits, long Misses, long Evictions,
    long Invalidations, bool CacheTraversedNodes, long TraversalNodesSeen, long TraversalAdmissions);

public sealed class QueryCache
{
    sealed record Entry(object Column, string Key, Row[] Rows, long Size);
    readonly Dictionary<(object, string), LinkedListNode<Entry>> map = [];
    readonly Dictionary<object, HashSet<string>> columnKeys = [];
    readonly LinkedList<Entry> lru = new();
    long maximum, bytes, hits, misses, evictions, invalidations;
    public bool TraversedNodes
    {
        get; set;
    }
    public long TraversalNodesSeen
    {
        get; internal set;
    }
    public long TraversalAdmissions
    {
        get; internal set;
    }
    public QueryCache(long maxBytes = 100_000_000, bool traversedNodes = false)
    {
        MaxBytes = maxBytes;
        TraversedNodes = traversedNodes;
    }
    public long MaxBytes
    {
        get => maximum; set
        {
            ArgumentOutOfRangeException.ThrowIfNegative(value);
            bool shrink = value < maximum;
            maximum = value;
            Trim();
            if (shrink)
                map.TrimExcess();
        }
    }
    public CacheInfo Info => new(maximum, bytes, map.Count, hits, misses, evictions, invalidations, TraversedNodes, TraversalNodesSeen, TraversalAdmissions);
    public Row[]? Get(object column, object? value)
    {
        if (maximum == 0 || !Values.Cacheable(value))
            return null;
        if (!map.TryGetValue((column, Values.CacheKey(value)), out var node))
        {
            misses++;
            return null;
        }
        hits++;
        lru.Remove(node);
        lru.AddLast(node);
        return node.Value.Rows;
    }
    public bool Put(object column, object? value, IEnumerable<Row> rows)
    {
        if (maximum == 0 || !Values.Cacheable(value))
            return false;
        string key = Values.CacheKey(value);
        Row[] found = rows.ToArray();
        long size = 512L + 2L * key.Length + 24 + 8L * found.Length;
        if (size > maximum)
            return false;
        if (map.Remove((column, key), out var previous))
        {
            bytes -= previous.Value.Size;
            lru.Remove(previous);
        }
        var node = lru.AddLast(new Entry(column, key, found, size));
        map[(column, key)] = node;
        if (!columnKeys.TryGetValue(column, out var keys))
            columnKeys[column] = keys = [];
        keys.Add(key);
        bytes += size;
        Trim();
        return true;
    }
    void Trim()
    {
        while (bytes > maximum && lru.First is { } node)
        {
            bytes -= node.Value.Size;
            map.Remove((node.Value.Column, node.Value.Key));
            var keys = columnKeys[node.Value.Column];
            keys.Remove(node.Value.Key);
            if (keys.Count == 0)
                columnKeys.Remove(node.Value.Column);
            lru.RemoveFirst();
            evictions++;
        }
        if (map.Count == 0)
            map.TrimExcess();
    }
    public void Invalidate(object column)
    {
        if (!columnKeys.Remove(column, out var keys))
            return;
        foreach (string key in keys)
        {
            var node = map[(column, key)];
            bytes -= node.Value.Size;
            lru.Remove(node);
            map.Remove((column, key));
            invalidations++;
        }
        if (map.EnsureCapacity(0) > Math.Max(64, map.Count * 2))
            map.TrimExcess();
    }
    public void Clear()
    {
        invalidations += map.Count;
        map.Clear();
        columnKeys.Clear();
        map.TrimExcess();
        lru.Clear();
        bytes = 0;
    }
}

public sealed record OrderKey(object Value) : IComparable<OrderKey>
{
    public int CompareTo(OrderKey? other)
    {
        if (other is null)
            return 1;
        bool a = Value is string, b = other.Value is string;
        return a != b ? a.CompareTo(b) : Values.Compare(Value, other.Value);
    }
}

public sealed class SkipList<T> where T : notnull
{
    const int MaxHeight = 16;
    sealed class Node(OrderKey? key, int height)
    {
        public readonly OrderKey? Key = key; public readonly HashSet<T> Ids = []; public readonly Node?[] Next = new Node?[height];
    }
    readonly Node head = new(null, MaxHeight);
    public int Height { get; private set; } = 1;
    Node[] Path(OrderKey key, Dictionary<OrderKey, HashSet<T>>? trace = null)
    {
        var path = Enumerable.Repeat(head, MaxHeight).ToArray();
        Node node = head;
        for (int lane = Height - 1; lane >= 0; lane--)
        {
            while (node.Next[lane] is { } next)
            {
                if (trace is not null)
                    trace[next.Key!] = next.Ids;
                if (next.Key!.CompareTo(key) >= 0)
                    break;
                node = next;
            }
            path[lane] = node;
        }
        return path;
    }
    public IReadOnlyCollection<T> Get(OrderKey key)
    {
        // A read needs no predecessor array or traversal dictionary.
        Node node = head;
        for (int lane = Height - 1; lane >= 0; lane--)
            while (node.Next[lane] is { } next && next.Key!.CompareTo(key) < 0)
                node = next;
        Node? found = node.Next[0];
        return found is not null && found.Key!.CompareTo(key) == 0 ? found.Ids : Array.Empty<T>();
    }
    public (IReadOnlyCollection<T> Rows, Dictionary<OrderKey, HashSet<T>> Trace) GetWithTrace(OrderKey key, bool trace = true)
    {
        var visited = new Dictionary<OrderKey, HashSet<T>>();
        Node? node = Path(key, trace ? visited : null)[0].Next[0];
        return (node is not null && node.Key!.CompareTo(key) == 0 ? node.Ids : Array.Empty<T>(), visited);
    }
    public void Insert(OrderKey key, T id)
    {
        Node[] path = Path(key);
        Node? existing = path[0].Next[0];
        if (existing is not null && existing.Key!.CompareTo(key) == 0)
        {
            existing.Ids.Add(id);
            return;
        }
        int height = 1;
        while (height < MaxHeight && Random.Shared.NextDouble() < .25)
            height++;
        Height = Math.Max(Height, height);
        var node = new Node(key, height);
        node.Ids.Add(id);
        for (int lane = 0; lane < height; lane++)
        {
            node.Next[lane] = path[lane].Next[lane];
            path[lane].Next[lane] = node;
        }
    }
    public void Remove(OrderKey key, T id)
    {
        Node[] path = Path(key);
        Node? node = path[0].Next[0];
        if (node is null || node.Key!.CompareTo(key) != 0 || !node.Ids.Remove(id))
            throw new KeyNotFoundException();
        if (node.Ids.Count != 0)
            return;
        for (int lane = 0; lane < node.Next.Length; lane++)
            path[lane].Next[lane] = node.Next[lane];
        while (Height > 1 && head.Next[Height - 1] is null)
            Height--;
    }
    public IEnumerable<(OrderKey Key, IReadOnlyCollection<T> Ids)> Entries()
    {
        for (Node? node = head.Next[0]; node is not null; node = node.Next[0])
            yield return (node.Key!, node.Ids);
    }
    public IReadOnlyList<OrderKey> LaneKeys(int lane)
    {
        var keys = new List<OrderKey>();
        for (Node? node = head.Next[lane]; node is not null; node = node.Next[lane])
            keys.Add(node.Key!);
        return keys;
    }
    public (List<IReadOnlyCollection<T>> Rows, Dictionary<OrderKey, HashSet<T>> Trace) GetMany(IReadOnlyList<OrderKey> keys, bool chained = true, bool trace = false)
    {
        var path = Enumerable.Repeat(head, MaxHeight).ToArray();
        var visited = new Dictionary<OrderKey, HashSet<T>>();
        var result = new List<IReadOnlyCollection<T>>();
        OrderKey? previous = null;
        foreach (OrderKey key in keys)
        {
            if (!chained || previous is null || key.CompareTo(previous) < 0)
                path = Path(key, trace ? visited : null);
            else
            {
                int top = 0;
                while (top < Height - 1)
                {
                    Node? next = path[top].Next[top];
                    if (trace && next is not null)
                        visited[next.Key!] = next.Ids;
                    if (next is null || next.Key!.CompareTo(key) >= 0)
                        break;
                    if (top == 2)
                    {
                        top = Height - 1;
                        break;
                    }
                    top++;
                }
                Node node = path[top];
                for (int lane = top; lane >= 0; lane--)
                {
                    Node saved = path[lane];
                    if (saved != head && node != saved && (node == head || node.Key!.CompareTo(saved.Key) < 0))
                        node = saved;
                    while (node.Next[lane] is { } next)
                    {
                        if (trace)
                            visited[next.Key!] = next.Ids;
                        if (next.Key!.CompareTo(key) >= 0)
                            break;
                        node = next;
                    }
                    path[lane] = node;
                }
            }
            Node? found = path[0].Next[0];
            result.Add(found is not null && found.Key!.CompareTo(key) == 0 ? found.Ids : Array.Empty<T>());
            previous = key;
        }
        return (result, visited);
    }
}

public sealed class Branch(bool ordered)
{
    internal readonly Dictionary<Cell, Row> Members = [];
    public SkipList<Cell>? Lanes { get; } = ordered ? new() : null;
    public IReadOnlyDictionary<Cell, Row> Entries => Members;
    public int Count => Members.Count;
    internal void Put(Row row, Cell cell)
    {
        if (!Members.ContainsKey(cell))
            Lanes?.Insert(new(cell.Value!), cell);
        Members[cell] = row;
    }
    internal Row Take(Cell cell, object? old)
    {
        Lanes?.Remove(new(old!), cell);
        Row row = Members[cell];
        Members.Remove(cell);
        return row;
    }
    internal IEnumerable<Row> Matching(object? value) => Lanes is null ? Members.Where(p => Values.Equal(value, p.Key.Value)).Select(p => p.Value) : Lanes.Get(new(value!)).Select(cell => Members[cell]);
}

public sealed class BranchCollection : IReadOnlyDictionary<string?, Branch>
{
    readonly Dictionary<string, Branch> source;
    internal BranchCollection(Dictionary<string, Branch> source) => this.source = source;
    static string Encode(string? key) => key is null ? "u" : "k" + key;
    public Branch this[string? key] => source[Encode(key)]; public int Count => source.Count;
    public IEnumerable<string?> Keys => source.Keys.Select(key => key == "u" ? null : key[1..]); public IEnumerable<Branch> Values => source.Values;
    public bool ContainsKey(string? key) => source.ContainsKey(Encode(key)); public bool TryGetValue(string? key, out Branch value) => source.TryGetValue(Encode(key), out value!);
    public IEnumerator<KeyValuePair<string?, Branch>> GetEnumerator() => source.Select(pair => new KeyValuePair<string?, Branch>(pair.Key == "u" ? null : pair.Key[1..], pair.Value)).GetEnumerator(); System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator() => GetEnumerator();
}

public sealed class ColumnTree
{
    // A tagged key keeps NULL separate from the first character of every possible string.
    const string Unordered = "u";
    static string Key(object? value) => Values.TreeKey(value) is { } key ? "k" + key : Unordered;
    readonly QueryCache? cache; readonly bool observe; readonly Action<Row, Cell>? onChange;
    readonly object cacheColumn = new();
    readonly Dictionary<string, Branch> branches = [];
    readonly Dictionary<string, HashSet<Cell>> dead = [];
    readonly Dictionary<string, Dictionary<Cell, Row>> pending = [];
    readonly HashSet<Cell> unsafeCells = [];
    public BranchCollection Branches => new(branches);
    public IReadOnlyCollection<Cell> Dead => dead.Values.SelectMany(x => x).ToArray();
    public IReadOnlyCollection<Cell> Pending => pending.Values.SelectMany(x => x.Keys).ToArray();
    public int Count => branches.Values.Sum(b => b.Count) - dead.Values.Sum(d => d.Count) + pending.Values.Sum(p => p.Count);
    internal int Estimate(object? value) => Values.TreeKey(value) is { } k ? (branches.GetValueOrDefault("k" + k)?.Count ?? 0) + (branches.GetValueOrDefault(Unordered)?.Count ?? 0) : Count;
    public ColumnTree(QueryCache? cache = null, bool observe = true, Action<Row, Cell>? onChange = null)
    {
        this.cache = cache;
        this.observe = observe;
        this.onChange = onChange;
    }
    public IReadOnlyList<Row> this[string? key] { get { string k = key is null ? Unordered : "k" + key; Live([k]); return branches.TryGetValue(k, out var b) ? b.Members.Values.ToArray() : []; } }
    public void ClearCache() => cache?.Invalidate(cacheColumn);
    bool CanCache(object? value) => cache is { MaxBytes: > 0 } && unsafeCells.Count == 0 && Values.Cacheable(value);
    void Safety(Cell cell)
    {
        if (Values.Cacheable(cell.Value))
            unsafeCells.Remove(cell);
        else
            unsafeCells.Add(cell);
    }
    public void Add(Row row, Cell cell)
    {
        ClearCache();
        string key = Key(cell.Value);
        PopPending(key, cell);
        Safety(cell);
        if (!branches.TryGetValue(key, out var branch))
            branches[key] = branch = new(key != Unordered);
        branch.Put(row, cell);
        Unqueue(key, cell);
        if (observe)
            cell.Tree = this;
    }
    public void QueueAdd(Row row, Cell cell)
    {
        string key = Key(cell.Value);
        if (branches.TryGetValue(key, out var b) && b.Members.ContainsKey(cell))
        {
            Add(row, cell);
            return;
        }
        ClearCache();
        Safety(cell);
        if (!pending.TryGetValue(key, out var group))
            pending[key] = group = [];
        group[cell] = row;
        if (observe)
            cell.Tree = this;
        int n = pending.Values.Sum(p => p.Count);
        if (n > Math.Max(64, Count - n))
            FlushPending();
    }
    Row? PopPending(string key, Cell cell)
    {
        if (!pending.TryGetValue(key, out var p) || !p.Remove(cell, out var row))
            return null;
        if (p.Count == 0)
            pending.Remove(key);
        return row;
    }
    public void FlushPending() => Flush(pending.Keys.ToArray());
    void Flush(IEnumerable<string> keys)
    {
        foreach (string key in keys)
        {
            if (!pending.Remove(key, out var p))
                continue;
            if (!branches.TryGetValue(key, out var b))
                branches[key] = b = new(key != Unordered);
            foreach (var (cell, row) in p)
                b.Put(row, cell);
        }
    }
    void Unqueue(string key, Cell cell)
    {
        if (dead.TryGetValue(key, out var d))
        {
            d.Remove(cell);
            if (d.Count == 0)
                dead.Remove(key);
        }
    }
    public void Remove(Cell cell)
    {
        ClearCache();
        unsafeCells.Remove(cell);
        string key = Key(cell.Value);
        if (PopPending(key, cell) is null)
        {
            Branch branch = branches[key];
            branch.Take(cell, cell.Value);
            if (branch.Count == 0)
                branches.Remove(key);
            Unqueue(key, cell);
        }
        if (observe)
            cell.Tree = null;
    }
    public void Bury(Cell cell)
    {
        string key = Key(cell.Value);
        if (pending.TryGetValue(key, out var p) && p.ContainsKey(cell))
        {
            Remove(cell);
            return;
        }
        ClearCache();
        unsafeCells.Remove(cell);
        if (!dead.TryGetValue(key, out var d))
            dead[key] = d = [];
        d.Add(cell);
        if (dead.Values.Sum(x => x.Count) > Math.Max(64, Count))
            Sweep();
    }
    public void Sweep() => Live(dead.Keys.Union(pending.Keys).ToArray());
    void Live(IEnumerable<string> keys)
    {
        foreach (string key in keys)
        {
            if (dead.Remove(key, out var cells))
            {
                Branch b = branches[key];
                foreach (Cell cell in cells)
                {
                    b.Take(cell, cell.Value);
                    if (observe)
                        cell.Tree = null;
                }
                if (b.Count == 0)
                    branches.Remove(key);
            }
            Flush([key]);
        }
    }
    internal void Changed(Cell cell, object? old)
    {
        string previous = Key(old), current = Key(cell.Value);
        Row? row = pending.TryGetValue(previous, out var p) ? p.GetValueOrDefault(cell) : null;
        onChange?.Invoke(row ?? branches[previous].Members[cell], cell);
        ClearCache();
        Safety(cell);
        if (row is not null)
        {
            PopPending(previous, cell);
            if (!pending.TryGetValue(current, out p))
                pending[current] = p = [];
            p[cell] = row;
            return;
        }
        bool deleted = dead.TryGetValue(previous, out var d) && d.Contains(cell);
        Branch oldBranch = branches[previous];
        row = oldBranch.Take(cell, old);
        if (oldBranch.Count == 0)
            branches.Remove(previous);
        if (!branches.TryGetValue(current, out var branch))
            branches[current] = branch = new(current != Unordered);
        branch.Put(row, cell);
        if (deleted)
        {
            unsafeCells.Remove(cell);
            Unqueue(previous, cell);
            if (!dead.TryGetValue(current, out d))
                dead[current] = d = [];
            d.Add(cell);
        }
    }
    void Remember(Branch branch, Dictionary<OrderKey, HashSet<Cell>> trace)
    {
        if (cache is null)
            return;
        cache.TraversalNodesSeen += trace.Count;
        foreach (var (key, cells) in trace)
            if (cache.Put(cacheColumn, key.Value, cells.Select(cell => branch.Members[cell])))
                cache.TraversalAdmissions++;
    }
    public List<Row> Find(object? value)
    {
        bool use = CanCache(value);
        if (use && cache!.Get(cacheColumn, value) is { } hit)
            return hit.ToList();
        var rows = FindRaw(value);
        if (use)
            cache!.Put(cacheColumn, value, rows);
        return rows;
    }
    List<Row> FindRaw(object? value)
    {
        string key = Key(value);
        if (key == Unordered)
        {
            string[] keys = value is null ? [Unordered] : branches.Keys.Union(pending.Keys).ToArray();
            Live(keys);
            return keys.Where(branches.ContainsKey).SelectMany(k => branches[k].Members).Where(p => Values.Equal(value, p.Key.Value)).Select(p => p.Value).ToList();
        }
        Live([key, Unordered]);
        var rows = new List<Row>();
        if (branches.TryGetValue(key, out var branch))
        {
            if (cache is { TraversedNodes: true } && CanCache(value))
            {
                var found = branch.Lanes!.GetWithTrace(new(value!));
                Remember(branch, found.Trace);
                rows.AddRange(found.Rows.Select(cell => branch.Members[cell]));
            }
            else
                rows.AddRange(branch.Matching(value));
        }
        if (branches.TryGetValue(Unordered, out var unkeyed))
            rows.AddRange(unkeyed.Matching(value));
        return rows;
    }
    public List<List<Row>> FindBatch(IEnumerable<object?> query, string order = "tree", bool chained = true)
    {
        if (order is not "tree" and not "input")
            throw new ArgumentException("order must be tree or input");
        var values = query.ToArray();
        var result = new List<Row>?[values.Length];
        var groups = new Dictionary<string, List<int>>();
        for (int i = 0; i < values.Length; i++)
        {
            if (CanCache(values[i]) && cache!.Get(cacheColumn, values[i]) is { } hit)
            {
                result[i] = hit.ToList();
                continue;
            }
            string key = Key(values[i]);
            if (key == Unordered)
            {
                result[i] = FindRaw(values[i]);
                continue;
            }
            if (!groups.TryGetValue(key, out var indices))
                groups[key] = indices = [];
            indices.Add(i);
        }
        foreach (var (key, indices) in groups)
        {
            Live([key, Unordered]);
            if (order == "tree")
                indices.Sort((a, b) => new OrderKey(values[a]!).CompareTo(new(values[b]!)));
            branches.TryGetValue(key, out var branch);
            branches.TryGetValue(Unordered, out var other);
            bool trace = cache is { TraversedNodes: true } && indices.All(i => CanCache(values[i]));
            bool reuse = chained && (order != "tree" || branch is null || indices.Count >= 8 && indices.Count * 64 >= branch.Count);
            var matches = branch?.Lanes!.GetMany(indices.Select(i => new OrderKey(values[i]!)).ToArray(), reuse, trace);
            if (trace && matches is { } m)
                Remember(branch!, m.Trace);
            for (int j = 0; j < indices.Count; j++)
            {
                int i = indices[j];
                result[i] = matches is { } found ? found.Rows[j].Select(cell => branch!.Members[cell]).ToList() : [];
                if (other is not null)
                    result[i]!.AddRange(other.Matching(values[i]));
            }
        }
        for (int i = 0; i < values.Length; i++)
            if (CanCache(values[i]))
                cache!.Put(cacheColumn, values[i], result[i]!);
        return result.Select(rows => rows!).ToList();
    }
}
