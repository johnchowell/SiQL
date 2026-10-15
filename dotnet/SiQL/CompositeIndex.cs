using System.Buffers.Binary;
using System.Numerics;
using System.Text;

namespace SiQL;

public class CompositeIndex
{
    readonly Table table;
    sealed class Group(string[] fields)
    {
        public readonly string[] Fields = fields; public readonly HashSet<Row> Rows = []; public readonly Dictionary<string, ColumnTree> Trees = []; public BigInteger Key; public byte[]? Signature;
    }
    readonly Dictionary<string, Group> groups = [];
    readonly List<Dictionary<string, HashSet<Group>>> postings = [];
    readonly Dictionary<Row, string> membership = [];
    readonly HashSet<Row> dirty = [];
    readonly HashSet<Row> mutable = [];
    readonly Dictionary<string, uint> symbols = [];
    bool schemaDirty = true, orderDirty = true;
    int[] widths = [];
    BigInteger[] minimums = [], maximums = [], averages = [];
    List<Group> ordered = [];
    int candidates, lastGroups, lastRange, lastBacktrack;
    BigInteger lastSeek;
    public bool Integer
    {
        get;
    }
    public CompositeIndex(Table table, bool integer)
    {
        this.table = table;
        Integer = integer;
        schemaDirty = false;
        foreach (var _ in table.Struct.Names)
            postings.Add([]);
        foreach (var row in table.Rows)
            Add(row);
    }
    static string Signature(string[] fields) => string.Join("|", fields.Select(f => $"{f.Length}:{f}"));
    static string Prefix(object? value) => value switch
    {
        null => "null",
        string s => "text:" + (Values.TreeKey(s) ?? ""),
        byte[] b => "byte:" + (b.Length == 0 ? -1 : b[0]),
        _ when Values.Numeric(value) && Values.TreeKey(value) is { } digit => "number:" + digit,
        _ => "*"
    };
    static byte[]? FullField(object? value)
    {
        byte[]? payload = value switch
        {
            null => [0],
            string s => [1, .. Values.Utf8SurrogatePass(s)],
            byte[] b => [3, .. b],
            _ => null
        };
        if (Values.Numeric(value))
        {
            try
            {
                var r = Values.Ratio(value!);
                payload = [2, .. Encoding.ASCII.GetBytes(FormattableString.Invariant($"{r.N}/{r.D}"))];
            }
            catch (ArgumentException) { return null; }
        }
        if (payload is null)
            return null;
        var result = new byte[4 + payload.Length];
        BinaryPrimitives.WriteUInt32BigEndian(result, (uint)payload.Length);
        payload.CopyTo(result, 4);
        return result;
    }
    string[] Fields(Row row) => table.Struct.Names.Select(name => Integer ? (FullField(row.Col(name).Value) is { } bytes ? Convert.ToHexString(bytes) : "*") : Prefix(row.Col(name).Value)).ToArray();
    void Invalidate(Group group)
    {
        foreach (var tree in group.Trees.Values)
            tree.ClearCache();
        group.Trees.Clear();
    }
    public void Close()
    {
        foreach (var g in groups.Values)
            Invalidate(g);
        foreach (var row in membership.Keys)
        {
            row.CompressedSignature = null;
            row.IntegerSignature = null;
        }
        groups.Clear();
        membership.Clear();
        dirty.Clear();
        mutable.Clear();
        ordered.Clear();
        postings.Clear();
    }
    public void SchemaChanged() => schemaDirty = true;
    public void Changed(Row row)
    {
        if (membership.ContainsKey(row))
            dirty.Add(row);
    }
    public void Add(Row row)
    {
        if (schemaDirty)
            return;
        var fields = Fields(row);
        string signature = Signature(fields);
        if (!groups.TryGetValue(signature, out var group))
        {
            groups[signature] = group = new(fields);
            for (int i = 0; i < fields.Length; i++)
            {
                if (!postings[i].TryGetValue(fields[i], out var matched))
                    postings[i][fields[i]] = matched = [];
                matched.Add(group);
            }
        }
        Invalidate(group);
        group.Rows.Add(row);
        membership[row] = signature;
        if (group.Signature is null)
        {
            if (Integer)
                group.Signature = fields.SelectMany(f => f == "*" ? new byte[4] : Convert.FromHexString(f)).ToArray();
            else
            {
                var bytes = new byte[fields.Length * 4];
                for (int i = 0; i < fields.Length; i++)
                {
                    uint id = 0;
                    if (fields[i] != "*")
                    {
                        if (!symbols.TryGetValue(fields[i], out id))
                            symbols[fields[i]] = id = (uint)symbols.Count + 1;
                    }
                    BinaryPrimitives.WriteUInt32LittleEndian(bytes.AsSpan(i * 4), id);
                }
                group.Signature = bytes;
            }
        }
        row.CompressedSignature = group.Signature;
        if (row.Content.Any(c => c.Value is byte[]))
            mutable.Add(row);
        else
            mutable.Remove(row);
        orderDirty = true;
    }
    public void Remove(Row row)
    {
        if (membership.Remove(row, out var signature))
        {
            var group = groups[signature];
            Invalidate(group);
            group.Rows.Remove(row);
            if (group.Rows.Count == 0)
            {
                groups.Remove(signature);
                for (int i = 0; i < group.Fields.Length; i++)
                {
                    var matched = postings[i][group.Fields[i]];
                    matched.Remove(group);
                    if (matched.Count == 0)
                        postings[i].Remove(group.Fields[i]);
                }
            }
        }
        dirty.Remove(row);
        mutable.Remove(row);
        row.CompressedSignature = null;
        row.IntegerSignature = null;
        orderDirty = true;
    }
    void Ensure()
    {
        if (schemaDirty)
        {
            Close();
            symbols.Clear();
            schemaDirty = false;
            foreach (var _ in table.Struct.Names)
                postings.Add([]);
            foreach (var row in table.Rows)
                Add(row);
        }
        foreach (var row in mutable.ToArray())
            if (Signature(Fields(row)) != membership[row])
                dirty.Add(row);
        foreach (var row in dirty.ToArray())
        {
            Remove(row);
            Add(row);
        }
        if (Integer && orderDirty)
            BuildOrder();
    }
    BigInteger Pack(IEnumerable<BigInteger> parts)
    {
        BigInteger key = 0;
        int i = 0;
        foreach (var part in parts)
        {
            key = (key << (8 * widths[i++])) | part;
        }
        return key;
    }
    BigInteger Code(string field, int i)
    {
        if (field == "*")
            return 0;
        byte[] source = Convert.FromHexString(field);
        if (source.Length > widths[i])
            return 0;
        var padded = new byte[widths[i]];
        source.CopyTo(padded, 0);
        return new(padded, isUnsigned: true, isBigEndian: true);
    }
    void BuildOrder()
    {
        widths = Enumerable.Range(0, table.Struct.Count).Select(i => Math.Max(4, groups.Values.Select(g => g.Fields[i] == "*" ? 0 : g.Fields[i].Length / 2).DefaultIfEmpty(0).Max())).ToArray();
        var all = groups.Values.ToArray();
        var codes = all.Select(g => g.Fields.Select(Code).ToArray()).ToArray();
        minimums = new BigInteger[widths.Length];
        maximums = new BigInteger[widths.Length];
        averages = new BigInteger[widths.Length];
        for (int i = 0; i < widths.Length; i++)
        {
            var vals = codes.Select(c => c[i]).ToArray();
            minimums[i] = vals.Length == 0 ? 0 : vals.Min();
            maximums[i] = vals.Length == 0 ? 0 : vals.Max();
            averages[i] = membership.Count == 0 ? 0 : codes.Select((c, j) => c[i] * all[j].Rows.Count).Aggregate(BigInteger.Zero, (a, b) => a + b) / membership.Count;
        }
        for (int i = 0; i < all.Length; i++)
        {
            all[i].Key = Pack(codes[i]);
            foreach (var row in all[i].Rows)
                row.IntegerSignature = all[i].Key;
        }
        ordered = all.OrderBy(g => g.Key).ToList();
        orderDirty = false;
    }
    int Lower(BigInteger key, bool upper = false)
    {
        int lo = 0, hi = ordered.Count;
        while (lo < hi)
        {
            int mid = (lo + hi) / 2;
            if (ordered[mid].Key < key || upper && ordered[mid].Key == key)
                lo = mid + 1;
            else
                hi = mid;
        }
        return lo;
    }
    public List<Row> Find(IReadOnlyDictionary<string, object?> criteria, string seek = "under")
    {
        if (seek is not "under" and not "lower")
            throw new ArgumentException("seek must be under or lower");
        Ensure();
        var selected = new HashSet<Group>();
        if (!Integer)
        {
            var routes = new List<HashSet<Group>>();
            foreach (var p in criteria)
            {
                string token = Prefix(p.Value);
                if (token == "*")
                    continue;
                int i = table.Struct.Col(p.Key);
                var route = new HashSet<Group>(postings[i].GetValueOrDefault(token) ?? []);
                route.UnionWith(postings[i].GetValueOrDefault("*") ?? []);
                routes.Add(route);
            }
            if (routes.Count == 0)
                selected.UnionWith(groups.Values);
            else
            {
                routes.Sort((a, b) => a.Count.CompareTo(b.Count));
                selected.UnionWith(routes[0]);
                foreach (var route in routes.Skip(1))
                    selected.IntersectWith(route);
            }
        }
        else
        {
            var lower = new BigInteger[widths.Length];
            var upper = new BigInteger[widths.Length];
            var guess = new BigInteger[widths.Length];
            var mask = new BigInteger[widths.Length];
            var desired = new BigInteger[widths.Length];
            bool impossible = false;
            for (int i = 0; i < widths.Length; i++)
            {
                string name = table.Struct.Names[i];
                byte[]? f = criteria.TryGetValue(name, out var value) ? FullField(value) : null;
                if (f is not null)
                {
                    string field = Convert.ToHexString(f);
                    if (!postings[i].ContainsKey(field))
                        impossible = true;
                    selected.UnionWith(postings[i].GetValueOrDefault("*") ?? []);
                    lower[i] = upper[i] = guess[i] = desired[i] = Code(field, i);
                    mask[i] = (BigInteger.One << (8 * widths[i])) - 1;
                }
                else
                {
                    lower[i] = minimums[i];
                    upper[i] = maximums[i];
                    guess[i] = BigInteger.Max(minimums[i], averages[i] - (maximums[i] - minimums[i]) / 4);
                }
            }
            BigInteger low = Pack(lower), high = Pack(upper);
            lastSeek = seek == "lower" ? low : BigInteger.Max(low, BigInteger.Min(high, Pack(guess)));
            lastRange = lastBacktrack = 0;
            if (!impossible)
            {
                int begin = Lower(low), end = Lower(high, true), jump = Math.Clamp(Lower(lastSeek), begin, end);
                BigInteger m = Pack(mask), want = Pack(desired);
                lastRange = end - begin;
                lastBacktrack = jump - begin;
                for (int i = jump; i < end; i++)
                    if ((ordered[i].Key & m) == want)
                        selected.Add(ordered[i]);
                for (int i = jump - 1; i >= begin; i--)
                    if ((ordered[i].Key & m) == want)
                        selected.Add(ordered[i]);
            }
        }
        candidates = selected.Sum(g => g.Rows.Count);
        lastGroups = selected.Count;
        var result = new List<Row>();
        foreach (var group in selected)
        {
            if (Integer)
            {
                result.AddRange(group.Rows.Where(row => criteria.All(p => Values.Equal(p.Value, row.Col(p.Key).Value))));
                continue;
            }
            HashSet<Row>? matched = null;
            foreach (var (column, value) in criteria)
            {
                if (!group.Trees.TryGetValue(column, out var tree))
                {
                    group.Trees[column] = tree = new(table.QueryCache, observe: false);
                    foreach (var row in group.Rows)
                        tree.Add(row, row.Col(column));
                }
                var found = tree.Find(value);
                if (matched is null)
                    matched = found.ToHashSet();
                else
                    matched.IntersectWith(found);
                if (matched.Count == 0)
                    break;
            }
            result.AddRange(matched ?? group.Rows);
        }
        return result;
    }
    public IReadOnlyDictionary<string, object?> Info
    {
        get
        {
            Ensure();
            var info = new Dictionary<string, object?> { ["enabled"] = true, ["active"] = true, ["rows"] = membership.Count, ["groups"] = groups.Count, ["symbols"] = symbols.Count, ["row_signature_width"] = Integer ? widths.Sum() : 4 * table.Struct.Count, ["interned_signature_bytes"] = groups.Values.Sum(g => g.Rows.First().CompressedSignature?.Length ?? 0), ["group_column_trees"] = groups.Values.Sum(g => g.Trees.Count), ["last_candidate_groups"] = lastGroups, ["last_candidate_rows"] = candidates };
            if (Integer)
            {
                info["mode"] = "integer";
                info["integer_bits"] = widths.Sum() * 8;
                info["last_range_groups"] = lastRange;
                info["last_backtracked_groups"] = lastBacktrack;
                info["last_seek_integer"] = lastSeek;
            }
            return info;
        }
    }
}

public sealed class CompressedIndex(Table table) : CompositeIndex(table, false);
public sealed class IntegerIndex(Table table) : CompositeIndex(table, true);
