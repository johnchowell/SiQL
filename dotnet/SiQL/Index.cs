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
    public void Discard(object column, object? value)
    {
        // Only the result for an equal value can change; equal numbers share one canonical key.
        if (!Values.Cacheable(value))
        {
            Invalidate(column);
            return;
        }
        string key = Values.CacheKey(value);
        if (!map.Remove((column, key), out var node))
            return;
        bytes -= node.Value.Size;
        lru.Remove(node);
        var keys = columnKeys[column];
        keys.Remove(key);
        if (keys.Count == 0)
            columnKeys.Remove(column);
        invalidations++;
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
    public List<IReadOnlyCollection<T>>? Between(OrderKey? low = null, OrderKey? high = null, bool lowInclusive = true,
        bool highInclusive = true, int limit = -1)
    {
        Node? node = low is null ? head.Next[0] : Path(low)[0].Next[0];
        if (low is not null && node is not null && !lowInclusive && node.Key!.CompareTo(low) == 0)
            node = node.Next[0];
        var result = new List<IReadOnlyCollection<T>>();
        int total = 0;
        for (; node is not null; node = node.Next[0])
        {
            if (high is not null && node.Key!.CompareTo(high) is var c && (highInclusive ? c > 0 : c >= 0))
                break;
            total += node.Ids.Count;
            if (limit >= 0 && total > limit)
                return null;
            result.Add(node.Ids);
        }
        return result;
    }
    public List<(OrderKey Key, IReadOnlyCollection<T> Ids)> Nearest(OrderKey? bound = null, int count = 1,
        bool descending = false, bool inclusive = true)
    {
        // Lanes link forward only, so a descending walk repeats the predecessor search: O(log n) per key.
        Node? node;
        if (bound is null)
        {
            node = head;
            if (descending)
                for (int lane = Height - 1; lane >= 0; lane--)
                    while (node.Next[lane] is { } next)
                        node = next;
            else
                node = node.Next[0];
        }
        else
        {
            Node before = Path(bound)[0];
            Node? at = before.Next[0];
            bool equal = at is not null && at.Key!.CompareTo(bound) == 0;
            node = descending ? (equal && inclusive ? at : before) : (equal && !inclusive ? at!.Next[0] : at);
        }
        var result = new List<(OrderKey, IReadOnlyCollection<T>)>();
        int total = 0;
        while (node is not null && node != head && total < count)
        {
            result.Add((node.Key!, node.Ids));
            total += node.Ids.Count;
            node = descending ? Path(node.Key!)[0] : node.Next[0];
        }
        return result;
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
    public int Estimate(object? value)
    {
        // Cells equal to the value, or an upper bound when custom values could also be equal.
        if (unsafeCells.Count != 0 || !Values.Cacheable(value))
            return Count;
        string key = Key(value);
        int queued = pending.TryGetValue(key, out var p) ? p.Count : 0;
        if (!branches.TryGetValue(key, out var branch))
            return queued;
        return queued + (branch.Lanes is null ? branch.Count : branch.Lanes.Get(new(value!)).Count);
    }
    public ColumnTree(QueryCache? cache = null, bool observe = true, Action<Row, Cell>? onChange = null)
    {
        this.cache = cache;
        this.observe = observe;
        this.onChange = onChange;
    }
    public IReadOnlyList<Row> this[string? key] { get { string k = key is null ? Unordered : "k" + key; Live([k]); return branches.TryGetValue(k, out var b) ? b.Members.Values.ToArray() : []; } }
    public void ClearCache() => cache?.Invalidate(cacheColumn);
    void Invalidate(object? value) => cache?.Discard(cacheColumn, value);
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
        Invalidate(cell.Value);
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
        Invalidate(cell.Value);
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
        Invalidate(cell.Value);
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
        Invalidate(cell.Value);
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
        Invalidate(old);
        Invalidate(cell.Value);
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
    static bool Digit(string key) => key.Length == 2 && char.IsAsciiDigit(key[1]);
    public List<Row>? FindRange(object? low = null, object? high = null, bool lowInclusive = true, bool highInclusive = true,
        int limit = -1)
    {
        // Rows whose value lies between bounds of one kind, compared as scans compare them. Null when the lanes
        // can't answer exactly (custom values or mixed bounds) or more than `limit` rows match.
        object[] bounds = new[] { low, high }.Where(bound => bound is not null).ToArray()!;
        if (bounds.Length == 0 || unsafeCells.Count != 0 || !bounds.All(Values.Cacheable) ||
            bounds.Select(bound => bound is string).Distinct().Count() != 1)
            return null;
        bool text = bounds[0] is string;
        var keys = branches.Keys.Union(pending.Keys).Where(key => key != Unordered).ToList();
        OrderKey? lower, upper;
        bool lowerInclusive, upperInclusive;
        if (text)
        {
            // Every string in a branch starts with its key, so whole branches fall outside the bounds.
            keys = keys.Where(key => !(high is not null && Values.Compare(key[1..], high) > 0) &&
                !(low is not null && Values.Compare(key[1..], Values.TreeKey(low)!) < 0)).ToList();
            (lower, lowerInclusive) = (new(low ?? ""), low is null || lowInclusive);
            (upper, upperInclusive) = (high is null ? null : new(high), highInclusive);
        }
        else
        {
            // Numbers sort before strings in a branch; first digits don't follow numeric order, so read each.
            keys = keys.Where(Digit).ToList();
            (lower, lowerInclusive) = (low is null ? null : new(low), lowInclusive);
            (upper, upperInclusive) = high is null ? (new OrderKey(""), false) : (new(high), highInclusive);
        }
        Live(keys.Append(Unordered).ToArray());
        var rows = new List<Row>();
        foreach (string key in keys)
        {
            if (!branches.TryGetValue(key, out var branch))
                continue;
            var found = branch.Lanes!.Between(lower, upper, lowerInclusive, upperInclusive, limit < 0 ? -1 : limit - rows.Count);
            if (found is null)
                return null;
            foreach (var cells in found)
                rows.AddRange(cells.Select(cell => branch.Members[cell]));
        }
        if (!text && branches.TryGetValue(Unordered, out var unkeyed))
        {
            // Infinities have no first digit.
            rows.AddRange(unkeyed.Members.Where(p => p.Key.Value is not null &&
                (low is null || Values.Match(p.Key.Value, lowInclusive ? ">=" : ">", low)) &&
                (high is null || Values.Match(p.Key.Value, highInclusive ? "<=" : "<", high))).Select(p => p.Value));
            if (limit >= 0 && rows.Count > limit)
                return null;
        }
        return rows;
    }
    public List<(object Value, List<Row> Rows)>? First(int count, bool descending = false)
    {
        // The leading non-null values in sort order with their rows, covering at least `count` rows (all rows when
        // fewer). Null when the lanes can't order the column: custom values, or numbers and strings together.
        if (unsafeCells.Count != 0)
            return null;
        var keys = branches.Keys.Union(pending.Keys).Where(key => key != Unordered).ToArray();
        Live(keys.Append(Unordered).ToArray());
        var ordered = keys.Where(branches.ContainsKey).ToDictionary(key => key, key => branches[key]);
        var loose = new List<(object Value, List<Row> Rows)>();
        if (branches.TryGetValue(Unordered, out var unkeyed))
            foreach (var group in unkeyed.Members.Where(p => p.Key.Value is not null).GroupBy(p => Values.CacheKey(p.Key.Value)))
                loose.Add((group.First().Key.Value!, group.Select(p => p.Value).ToList()));
        bool numbers = loose.Count > 0 || ordered.Any(p => Digit(p.Key) && p.Value.Lanes!.Nearest()[0].Key.Value is not string);
        bool strings = ordered.Any(p => !Digit(p.Key) || p.Value.Lanes!.Nearest(null, 1, true)[0].Key.Value is string);
        if (numbers && strings)
            return null;
        var found = new List<(object Value, List<Row> Rows)>();
        if (numbers)
        {
            // First digits don't follow numeric order: take each branch's leading values, then merge them.
            foreach (var (key, branch) in ordered.Where(p => Digit(p.Key)))
                found.AddRange(branch.Lanes!.Nearest(descending ? new OrderKey("") : null, count, descending, false)
                    .Where(p => p.Key.Value is not string).Select(p => (p.Key.Value, p.Ids.Select(cell => branch.Members[cell]).ToList())));
            found.AddRange(loose);
            var comparer = Comparer<object>.Create(Values.Compare);
            found = (descending ? found.OrderByDescending(p => p.Value, comparer) : found.OrderBy(p => p.Value, comparer)).ToList();
        }
        else
        {
            // Every string in a branch starts with its key, so branches in key order give the overall order.
            var comparer = Comparer<string>.Create((a, b) => Values.Compare(a[1..], b[1..]));
            foreach (string key in descending ? ordered.Keys.OrderDescending(comparer) : ordered.Keys.Order(comparer))
            {
                int held = found.Sum(p => p.Rows.Count);
                if (held >= count)
                    break;
                var branch = ordered[key];
                found.AddRange(branch.Lanes!.Nearest(descending ? null : new OrderKey(""), count - held, descending)
                    .Select(p => (p.Key.Value, p.Ids.Select(cell => branch.Members[cell]).ToList())));
            }
        }
        var result = new List<(object Value, List<Row> Rows)>();
        int total = 0;
        foreach (var entry in found)
        {
            if (total >= count)
                break;
            result.Add(entry);
            total += entry.Rows.Count;
        }
        return result;
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
