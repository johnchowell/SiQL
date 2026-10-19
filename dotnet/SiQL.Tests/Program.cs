using System.Collections;
using System.Net;
using System.Net.Http.Json;
using System.Reflection;
using System.Text;
using System.Text.Json;
using SiQL;

int assertions = 0;
void Assert(bool condition, string message) { assertions++; if (!condition) throw new Exception(message); }
void Equal(object? actual, object? expected, string name) { Assert(Values.Equal(actual, expected), $"{name}\nactual: {PythonLiteral.Write(actual)}\nexpected: {PythonLiteral.Write(expected)}"); }
Dictionary<string, object?> Dict(string literal) => Values.DictionaryEntries((IDictionary)PythonLiteral.Read(literal)!).ToDictionary(p => (string)p.Key, p => p.Value);
string temporary = Path.Combine(Path.GetTempPath(), "siql-dotnet-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(temporary);
try
{
    string NewFile() => Path.Combine(temporary, Guid.NewGuid().ToString("N") + ".siql");
    // Fast primitive comparisons must retain Python's Unicode/numeric boundaries.
    Assert(Values.Compare("\U00010000", "\ue000") > 0, "Unicode scalar order above BMP");
    Assert(Values.Compare("\ud800", "\U00010000") < 0, "unpaired surrogate order");
    Assert(Values.Compare("same", "same-longer") < 0, "string prefix order");
    Assert(!Values.Equal(double.NaN, double.NaN), "NaN never equals itself");
    Assert(Values.Equal(-0.0, 0.0), "signed zero equality");
    Assert(Values.Compare(long.MinValue, long.MaxValue) < 0, "signed integer limits");
    Assert(Values.Compare(ulong.MaxValue, long.MaxValue) > 0, "mixed unsigned integer limits");
    Assert(Values.Compare(9007199254740993L, 9007199254740992d) > 0, "mixed comparison retains precision");
    Assert(!Values.Equal(9007199254740993L, 9007199254740992d), "mixed equality retains precision");
    Assert(Values.Equal(1, true) && Values.Equal(1L, 1d), "numeric equivalence across types");
    try { Values.Compare(double.NaN, 1d); throw new Exception("NaN ordered"); }
    catch (ArgumentException) { }
    foreach (long budget in new long[] { 0, 100_000_000 })
    {
        var offsets = new Table(file: NewFile(), cacheMaxBytes: budget);
        offsets.AddCols(new Dictionary<string, ColumnType> { ["n"] = ColumnType.Int });
        var expected = Enumerable.Range(0, 600).ToList();
        new TableDiff(expected.Select(i => (DiffOp)new AddRow(i, new Dictionary<string, object?> { ["n"] = i }))).Apply(offsets);
        offsets.Find("n", 0);
        var rng = new Random(42);
        for (int step = 0; step < 450; step++)
        {
            int position = rng.Next(expected.Count);
            new TableDiff([new DropRow(position)]).Apply(offsets);
            expected.RemoveAt(position);
            if (step % 7 == 0)
            {
                offsets.Add(new Dictionary<string, object?> { ["n"] = 1000 + step });
                expected.Add(1000 + step);
            }
            if (step % 13 == 0)
            {
                string saved = File.ReadAllText(offsets.FilePath);
                try
                {
                    new TableDiff([new DropRow(-1), new AddRow(0, new Dictionary<string, object?> { ["absent"] = 1 })]).Apply(offsets);
                    throw new Exception("Expected rollback failure");
                }
                catch (KeyNotFoundException) { }
                Assert(File.ReadAllText(offsets.FilePath) == saved, "offset rollback leaves file unchanged");
            }
            int probe = rng.Next(expected.Count);
            Equal(offsets.Find("n", expected[probe]), new[] { probe }, "offset lookup after eager delete/append");
        }
        Assert(offsets.Index!["n"].Dead.Count == 0, "eager cleanup stays immediate");
        new TableDiff([new AddRow(1, new Dictionary<string, object?> { ["n"] = -1 })]).Apply(offsets);
        expected.Insert(1, -1);
        Equal(offsets.FindBatch("n", expected.Cast<object?>()), Enumerable.Range(0, expected.Count).Select(i => new[] { i }).ToList(), "offset reset after middle insert");
        Equal(new Table(file: offsets.FilePath).Rows.Select(r => r.Col("n").Value).ToList(), expected, "offset persistence");
    }
    var structure = new RowStruct(new Dictionary<string, ColumnType> { { "x", ColumnType.Any } });
    var initialRow = new Row(structure, new object?[] { 1 });
    var initialTable = new Table(new[] { initialRow }, file: NewFile());
    Assert(ReferenceEquals(initialTable.Struct, structure) && ReferenceEquals(initialTable.Rows[0], initialRow), "initial rows preserve shared identity");
    initialRow.Col("x").Value = 2;
    Equal(initialTable.Find("x", 2), new[] { 0 }, "initial row observers");
    var diffEquality = new TableDiff([new AddRow(0, Dict("{'x':[1,{'a':2}]}")), new SetCell(0, "x", 1)]);
    Assert(diffEquality.Equals(TableDiff.Loads(diffEquality.Dumps())), "diff value equality");
    Assert(new SetCell(0, "x", 1).Equals(new SetCell(0, "x", 1.0)), "numeric-equivalent diff values");
    var table = new Table(file: NewFile(), lazyAdd: true, lazyDelete: true);
    table.AddCols(new Dictionary<string, ColumnType> { { "name", ColumnType.Str }, { "age", ColumnType.Int } });
    table.Add(Dict("{'name': 'Ann', 'age': 31}"));
    table.Add(Dict("{'name': 'Bob', 'age': 25}"));
    Assert(table.Index!["name"].Pending.Count == 2, "lazy add deferred");
    Equal(table.Find("name", "Ann"), new[] { 0 }, "find pending");
    Assert(table.Index["name"].Pending.Count == 1, "branch-local flush");
    Assert(ReferenceEquals(table.Rows[0].Col("name"), table.Cols["name"][0]), "shared cells");
    table.Rows[1].Col("name").Value = "Zed";
    Equal(table.Find("name", "Zed"), new[] { 1 }, "direct pending edit");
    table.Find("name", "Zed");
    Assert(table.CacheInfo.Hits > 0, "result cache hit");
    table.CompressedSearch = true;
    Equal(table.FindWhere(Dict("{'name':'Zed','age':25}")), new[] { 1 }, "prefix composite");
    table.IntegerSearch = true;
    Equal(table.FindWhere(Dict("{'name':'Zed','age':25}")), new[] { 1 }, "integer composite");
    table.Rows[1].Col("age").Value = 42;
    Equal(table.FindWhere(Dict("{'name':'Zed','age':42}")), new[] { 1 }, "integer direct edit");
    string before = File.ReadAllText(table.FilePath);
    try
    {
        new TableDiff([new DropRow(0), new AddRow(0, Dict("{'missing':1}"))]).Apply(table);
        throw new Exception("rollback did not throw");
    }
    catch (KeyNotFoundException) { }
    Equal(table.Find("name", "Ann"), new[] { 0 }, "rollback state");
    Assert(File.ReadAllText(table.FilePath) == before, "rollback file untouched");
    table.CacheMaxBytes = 1000;
    Assert(table.CacheInfo.Bytes <= 1000, "cache shrink");
    table.CacheMaxBytes = 0;
    Assert(table.CacheInfo.Entries == 0, "cache disable");
    table.CacheTraversedNodes = true;
    table.CacheMaxBytes = 100_000;
    for (int i = 0; i < 100; i++)
        table.Add(new Dictionary<string, object?> { { "name", $"item{i:000}" }, { "age", i } });
    Equal(table.FindBatch("name", new object?[] { "item090", "item001", "absent", "item090" }), new List<object?> { new[] { 92 }, new[] { 3 }, Array.Empty<int>(), new[] { 92 } }, "chained batch input restoration");
    Assert(table.CacheInfo.TraversalNodesSeen > 0, "traversal admission");
    table.Purge();
    Assert(table.Index.Values.All(n => n.Pending.Count == 0 && n.Dead.Count == 0), "purge");
    table.IntegerSearch = false;
    table.CompressedSearch = false;
    table.ClearCache();
    table.Find("name", "Ann");
    long hits = table.CacheInfo.Hits;
    table.Rows[0].Col("age").Value = 999;
    table.Find("name", "Ann");
    Assert(table.CacheInfo.Hits == hits + 1, "unrelated-column cache preserved");
    table.Rows[0].Col("age").Value = new MatchesEverything();
    Equal(table.Find("age", null), new[] { 0 }, "reflected custom equality NULL");
    Equal(table.Find("age", 123456), new[] { 0 }, "reflected custom equality integer");
    table.Rows[0].Col("age").Value = 31;
    foreach (bool integerMode in new[] { false, true })
    {
        table.IntegerSearch = integerMode;
        table.CompressedSearch = !integerMode;
        byte[] data = [1, 2];
        table.AddCols(new Dictionary<string, ColumnType> { { "blob", ColumnType.Bytes } });
        table.Rows[0].Col("blob").Value = data;
        Equal(table.FindWhere(new Dictionary<string, object?> { { "name", "Ann" }, { "blob", new byte[] { 1, 2 } } }), new[] { 0 }, "binary composite before edit");
        data[1] = 3;
        Equal(table.FindWhere(new Dictionary<string, object?> { { "name", "Ann" }, { "blob", new byte[] { 1, 3 } } }), new[] { 0 }, "binary composite in-place edit");
        new TableDiff([new DropCol("blob")]).Apply(table);
    }
    table.IntegerSearch = false;
    table.CompressedSearch = false;
    var lru = new QueryCache(2000);
    var ns = new object();
    lru.Put(ns, "a", new[] { table.Rows[0] });
    lru.Put(ns, "b", new[] { table.Rows[1] });
    lru.Get(ns, "a");
    lru.Put(ns, "c", new[] { table.Rows[0] });
    lru.Put(ns, "d", new[] { table.Rows[0] });
    Assert(lru.Info.Bytes <= 2000 && lru.Info.Evictions > 0, "rolling byte-budget eviction");
    long retained = lru.Info.Bytes;
    Assert(!lru.Put(ns, "oversized", Enumerable.Repeat(table.Rows[0], 1000)) && lru.Info.Bytes == retained, "oversized result preserves cache");
    lru.Invalidate(ns);
    Assert(lru.Info.Entries == 0, "namespace invalidation");
    table.Tree = false;
    Equal(table.Find("name", "Ann"), new[] { 0 }, "tree off");
    table.Tree = true;
    Equal(Values.Match(null, ">=", null), false, "NULL comparisons");
    Equal(Values.Match(1, "<", double.PositiveInfinity), true, "infinity comparisons");
    foreach (string literal in new[] { "{'a': {'b': [1, 2]}}", "{None: 'null', True: 'bool'}", "(1+2j)", "0xff", "-0b10", "'😀\\n\\x00'", "b'a\\x00\\xff'", "set()" })
    {
        object? value = PythonLiteral.Read(literal);
        Equal(PythonLiteral.Read(PythonLiteral.Write(value)), value, "literal roundtrip " + literal);
    }
    Assert(Like.Matches("😀", "_") && !Like.Matches("😀", "__"), "Unicode LIKE scalar width");
    Assert(Like.Matches("İı", "ii") && Like.Matches("ſK", "sk"), "Unicode LIKE case folding");
    Equal(Search.Scan(new[] { new Cell(null), new Cell(1), new Cell(2) }, ScanOp.Ge, 1), new[] { 1, 2 }, "scan helper");
    Equal(Search.ValuesOf(new[] { new Cell(1), new Cell("x") }).ToList(), new List<object?> { 1, "x" }, "values helper");
    Assert(Csv.Read("x\r\n\r\n")[1].Count == 0 && Csv.Read("x\r\n\"\"\r\n")[1].Count == 1, "CSV blank versus quoted empty");
    var configPath = Path.Combine(temporary, "siql.toml");
    File.WriteAllText(configPath, ServerConfig.Template);
    var config = ServerConfig.Load(configPath);
    Assert(config.Port == 8642 && config.DataDir == Path.Combine(temporary, "data"), "TOML config");
    var interpreter = new Interpreter(Path.Combine(temporary, "csv"));
    interpreter.Execute("CREATE TABLE p (name str, age int)");
    interpreter.Execute("INSERT INTO p VALUES ('a,b',1), ('a''b',NULL)");
    string csv = Path.Combine(temporary, "data.csv");
    interpreter.Execute("EXPORT p TO '" + csv.Replace("'", "''") + "'");
    interpreter.Execute("IMPORT '" + csv.Replace("'", "''") + "' INTO q");
    Equal(interpreter.Execute("SELECT * FROM q").Rows.Select(r => r.ToList()).ToList(), interpreter.Execute("SELECT * FROM p").Rows.Select(r => r.ToList()).ToList(), "CSV roundtrip");
    File.WriteAllText(csv, "name,age\r\na,not-an-int\r\n");
    int beforeImport = interpreter.Database.Table("p").Rows.Count;
    try
    {
        interpreter.Execute("IMPORT '" + csv.Replace("'", "''") + "' INTO p");
        throw new Exception("bad CSV accepted");
    }
    catch (QueryError) { }
    Assert(interpreter.Database.Table("p").Rows.Count == beforeImport, "bad CSV changes nothing");
    var crash = new Table(file: NewFile());
    crash.AddCols(new Dictionary<string, ColumnType> { { "x", ColumnType.Int } });
    crash.Add(Dict("{'x':1}"));
    File.AppendAllText(crash.FilePath, "('+r', 1, {'x':");
    var recovered = new Table(file: crash.FilePath);
    Assert(recovered.Rows.Count == 1 && File.ReadAllText(crash.FilePath).EndsWith('\n'), "partial trailing line recovery");
    recovered.Compact();
    Assert(new Table(file: crash.FilePath).Rows.Count == 1, "compaction reload");
    var restricted = new Interpreter(interpreter.Database, false);
    foreach (string query in new[] { "USE '..'", "IMPORT 'bad' INTO p", "EXPORT p TO 'bad'" })
    {
        try
        {
            restricted.Execute(query);
            throw new Exception("restricted file statement permitted");
        }
        catch (QueryError) { assertions++; }
    }
    using (var server = new SiQLServer(new()
    {
        Host = "127.0.0.1",
        Port = 0,
        DataDir = Path.Combine(temporary, "server"),
        Token = "secret",
        MaxBody = 10000
    }))
    {
        using var stop = new CancellationTokenSource();
        var serve = server.ServeAsync(stop.Token);
        using var http = new HttpClient { BaseAddress = server.Address };
        Assert((await http.GetAsync("health")).StatusCode == HttpStatusCode.OK, "health");
        Assert((await http.GetAsync("tables")).StatusCode == HttpStatusCode.Unauthorized, "auth");
        http.DefaultRequestHeaders.Authorization = new("Bearer", "secret");
        Task<HttpResponseMessage> Post(string query) => http.PostAsync("query", new StringContent(JsonSerializer.Serialize(new
        {
            query
        }), Encoding.UTF8, "application/json"));
        var response = await Post("CREATE TABLE t (x int); INSERT INTO t VALUES (1); SELECT * FROM t");
        Assert(response.StatusCode == HttpStatusCode.OK, "HTTP query " + await response.Content.ReadAsStringAsync());
        response = await Post("INSERT INTO t VALUES (2); SELECT * FROM missing");
        Assert(response.StatusCode == HttpStatusCode.BadRequest, "HTTP partial failure");
        using (var body = JsonDocument.Parse(await response.Content.ReadAsStringAsync()))
            Assert(body.RootElement.GetProperty("results").GetArrayLength() == 1, "HTTP completed results");
        response = await Post("CREATE TABLE wont (x int); SELEC");
        Assert(response.StatusCode == HttpStatusCode.BadRequest, "HTTP parse before execution");
        response = await Post("USE '..'");
        Assert(response.StatusCode == HttpStatusCode.BadRequest, "HTTP file sandbox");
        response = await http.PostAsync("query", new StringContent("not json", Encoding.UTF8, "application/json"));
        Assert(response.StatusCode == HttpStatusCode.BadRequest, "HTTP bad JSON");
        response = await http.PostAsync("query", new StringContent(new string('x', 10001), Encoding.UTF8, "application/json"));
        Assert(response.StatusCode == HttpStatusCode.RequestEntityTooLarge, "HTTP request body limit");
        response = await Post("USE '..'");
        Assert(response.StatusCode == HttpStatusCode.BadRequest, "HTTP file restrictions retained");
        Assert((await http.GetAsync("missing")).StatusCode == HttpStatusCode.NotFound, "HTTP missing route");
        stop.Cancel();
        await serve;
    }
    Assert(!AppDomain.CurrentDomain.GetAssemblies().Any(a => a.GetName().Name!.Contains("Python", StringComparison.OrdinalIgnoreCase)), "No Python runtime loaded");
    Console.WriteLine($"Standalone .NET checks passed: {assertions} assertions.");
    if (args.Length > 0)
    {
        string fixtures = args[0];
        using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(fixtures, "fixtures.json")));
        JsonElement root = document.RootElement;
        Assert(root.GetProperty("version").GetString() == SiqlVersion.Value, "Python and .NET versions agree");
        var sql = new Interpreter(Path.Combine(temporary, "sql-parity"));
        foreach (var item in root.GetProperty("sql").EnumerateArray())
        {
            string query = item.GetProperty("query").GetString()!;
            if (item.TryGetProperty("error", out _))
            {
                try
                {
                    sql.Execute(query);
                    throw new Exception("Expected SQL error: " + query);
                }
                catch (QueryError) { assertions++; }
                continue;
            }
            Result result = sql.Execute(query);
            Equal(result.Columns.ToList(), item.GetProperty("columns").EnumerateArray().Select(c => c.GetString()).ToList(), query + " columns");
            Equal(result.Rows.Select(r => r.ToList()).ToList(), PythonLiteral.Read(item.GetProperty("rows").GetString()!), query + " rows");
            Assert(result.RowCount == item.GetProperty("rowcount").GetInt32(), query + " rowcount");
            Assert(result.Message == item.GetProperty("message").GetString(), query + " message: " + result.Message);
        }
        foreach (var mode in root.GetProperty("core").EnumerateArray())
        {
            var options = mode.GetProperty("options");
            bool Option(string key, bool fallback = false) => options.TryGetProperty(key, out var v) ? v.GetBoolean() : fallback;
            var t = new Table(file: NewFile(), tree: Option("tree", true), lazyDelete: Option("lazy_delete"), lazyAdd: Option("lazy_add"), compressedSearch: Option("compressed_search"), integerSearch: Option("integer_search"));
            t.AddCols(new Dictionary<string, ColumnType> { { "name", ColumnType.Str }, { "age", ColumnType.Int }, { "data", ColumnType.Any } });
            foreach (var step in mode.GetProperty("steps").EnumerateArray())
            {
                TableDiff.Loads(step.GetProperty("op").GetString()!).Apply(t);
                foreach (var check in step.GetProperty("checks").EnumerateArray())
                    Equal(t.FindWhere(Dict(check.GetProperty("criteria").GetString()!)), check.GetProperty("expected").EnumerateArray().Select(x => x.GetInt32()).ToList(), "mutation " + step.GetProperty("op").GetString());
            }
            Equal(t.Rows.Select(r => r.Content.Select(c => c.Value).ToList()).ToList(), PythonLiteral.Read(mode.GetProperty("snapshot").GetProperty("rows").GetString()!), "core snapshot");
            if (mode.TryGetProperty("integer_keys", out var keys))
            {
                _ = t.IntegerInfo;
                Equal(t.Rows.Select(r => r.IntegerSignature!.Value.ToString()).ToList(), keys.EnumerateArray().Select(k => k.GetString()).ToList(), "identical packed full-row integers");
                Assert((int)t.IntegerInfo["integer_bits"]! == mode.GetProperty("integer_bits").GetInt32(), "identical packed width");
            }
        }
        foreach (var literal in root.GetProperty("literals").EnumerateArray())
        {
            object? value = PythonLiteral.Read(literal.GetString()!);
            Equal(PythonLiteral.Read(PythonLiteral.Write(value)), value, "cross-language literal");
        }
        foreach (var item in root.GetProperty("diffs").EnumerateArray())
        {
            Table FromSnapshot(JsonElement snapshot)
            {
                var t = new Table(file: NewFile());
                string[] names = snapshot.GetProperty("names").EnumerateArray().Select(n => n.GetString()!).ToArray();
                string[] types = snapshot.GetProperty("types").EnumerateArray().Select(n => n.GetString()!).ToArray();
                t.AddCols(names.Select((n, i) => (n, i)).ToDictionary(p => p.n, p => Values.TypeFromName(types[p.i])));
                foreach (IList row in (IList)PythonLiteral.Read(snapshot.GetProperty("rows").GetString()!)!)
                    t.Add(names.Select((n, i) => (n, i)).ToDictionary(p => p.n, p => row[p.i]));
                return t;
            }
            var old = FromSnapshot(item.GetProperty("old"));
            var next = FromSnapshot(item.GetProperty("new"));
            var diff = TableDiff.Between(old, next);
            Equal(diff.Dumps(), item.GetProperty("ops").GetString(), "exact diff operations");
            diff.Apply(old);
            Equal(old.Rows.Select(r => r.Content.Select(c => c.Value).ToList()).ToList(), next.Rows.Select(r => r.Content.Select(c => c.Value).ToList()).ToList(), "diff final state");
        }
        string input = Path.Combine(temporary, "cross.siql");
        File.Copy(Path.Combine(fixtures, "python.siql"), input);
        var cross = new Table(file: input);
        Equal(cross.Rows.Select(r => r.Content.Select(c => c.Value).ToList()).ToList(), PythonLiteral.Read(root.GetProperty("persisted").GetProperty("rows").GetString()!), "Python to C# file");
        cross.Compact();
        File.Copy(input, Path.Combine(fixtures, "csharp.siql"), true);
        Console.WriteLine($"Python/C# parity passed: {assertions} assertions across all fixtures.");
    }
}
finally { Directory.Delete(temporary, true); }

sealed class MatchesEverything
{
    public override bool Equals(object? other) => true;
    public override int GetHashCode() => 0;
}
