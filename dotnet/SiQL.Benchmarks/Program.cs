using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using SiQL;

// Same fixture and phases as benchmarks/crud_review.py. Run a Release build.
Dictionary<string, double> Run(int size, int count)
{
    string folder = Path.Combine(Path.GetTempPath(), "siql-crud-" + Guid.NewGuid().ToString("N"));
    Directory.CreateDirectory(folder);
    try
    {
        var table = new Table(file: Path.Combine(folder, "table.siql"), cacheMaxBytes: 0);
        var schema = new Dictionary<string, ColumnType> { ["name"] = ColumnType.Str, ["n"] = ColumnType.Int };
        table.AddCols(schema);
        var names = Enumerable.Range(0, size).Select(i => $"name{i:D8}").ToArray();
        var probes = Enumerable.Range(0, count).Select(i => i * 7919 % size).ToArray();
        var times = new Dictionary<string, double>();
        void Measure(string label, int n, Action action)
        {
            long start = Stopwatch.GetTimestamp();
            action();
            times[label] = Stopwatch.GetElapsedTime(start).TotalMicroseconds / n;
        }
        Measure("Create", size, () =>
        {
            for (int i = 0; i < size; i++)
                table.Add(new Dictionary<string, object?> { ["name"] = names[i], ["n"] = i });
        });
        table.Find("n", 0);
        Measure("Read (str)", count, () => { foreach (int i in probes) table.Find("name", names[i]); });
        Measure("Read (int)", count, () => { foreach (int i in probes) table.Find("n", i); });
        Measure("Update", count, () => { foreach (int i in probes) table.EditByName("name", names[i], names[i] + "!"); });
        Measure("Delete + lookup", count, () =>
        {
            foreach (int i in probes)
            {
                var found = table.Find("name", names[i] + "!");
                if (found.Count != 1) throw new Exception("Invalid delete lookup");
                new TableDiff([new DropRow(found[0])]).Apply(table);
            }
        });
        var removed = probes.ToHashSet();
        var expected = Enumerable.Range(0, size).Where(i => !removed.Contains(i)).ToArray();
        void Verify(Table t)
        {
            if (!t.Rows.Select(r => Convert.ToInt32(r.Col("n").Value)).SequenceEqual(expected))
                throw new Exception("Invalid final rows");
        }
        Verify(table);
        Verify(new Table(file: table.FilePath, tree: false));
        var batch = new Table(file: Path.Combine(folder, "batch.siql"), cacheMaxBytes: 0);
        batch.AddCols(schema);
        var ops = Enumerable.Range(0, size).Select(i => (DiffOp)new AddRow(i,
            new Dictionary<string, object?> { ["name"] = names[i], ["n"] = i })).ToList();
        Measure("Batch create", size, () => new TableDiff(ops).Apply(batch));
        if (batch.Rows.Count != size) throw new Exception("Invalid batch rows");
        return times;
    }
    finally { Directory.Delete(folder, true); }
}
string output = args.Length > 0 ? args[0] : "build/crud-dotnet.json";
int repeats = args.Length > 1 ? int.Parse(args[1]) : 5;
Run(300, 30);
var results = new Dictionary<string, object>();
foreach (int size in new[] { 10000, 50000 })
{
    var runs = Enumerable.Range(0, repeats).Select(_ => Run(size, 500)).ToArray();
    var medians = runs[0].Keys.ToDictionary(k => k, k =>
    {
        var sorted = runs.Select(r => r[k]).Order().ToArray();
        return (sorted[(repeats - 1) / 2] + sorted[repeats / 2]) / 2;
    });
    results[size.ToString()] = new { runs_us = runs, median_us = medians };
    Console.WriteLine($"{size}: {JsonSerializer.Serialize(medians)}");
}
Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(output))!);
File.WriteAllText(output, JsonSerializer.Serialize(new
{
    runtime = RuntimeInformation.FrameworkDescription, platform = RuntimeInformation.OSDescription,
    utc = DateTimeOffset.UtcNow, cache_bytes = 0, repeats, queries = 500, results
}, new JsonSerializerOptions { WriteIndented = true }) + "\n");
