using System.Diagnostics;
using System.Net;
using System.Buffers;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Numerics;
using Tomlyn;
using Tomlyn.Model;

namespace SiQL;

public sealed record ServerConfig
{
    public string Host { get; set; } = "127.0.0.1";
    public int Port { get; set; } = 8642;
    public string DataDir { get; set; } = "data";
    public string? Token
    {
        get; set;
    }
    public long MaxBody { get; set; } = 1_048_576;
    public string LogLevel { get; set; } = "INFO";
    public string? UnixSocket { get; set; }
    public bool AdminEnabled { get; set; } = true;
    public int HistoryLimit { get; set; } = 100;
    public string? CorsOrigin { get; set; }
    public const string Template = """
        # SiQL server configuration
        [server]
        host = "127.0.0.1"
        port = 8642
        data_dir = "data"
        # token = "..."  # SIQL_TOKEN overrides this in the CLI
        max_body = 1048576
        log_level = "INFO"
        # unix_socket = "/run/siql/siql.sock"
        # admin_enabled = true
        # history_limit = 100
        # cors_origin = "*"
        """ + "\n";
    public void Validate()
    {
        if (Port < 0 || Port > 65535)
            throw new ArgumentException($"Invalid port: {Port}");
        if (MaxBody < 0)
            throw new ArgumentException($"Invalid max_body: {MaxBody}");
        if (HistoryLimit < 0)
            throw new ArgumentException($"Invalid history_limit: {HistoryLimit}");
        if (UnixSocket == "")
            UnixSocket = null;
        if (CorsOrigin == "")
            CorsOrigin = null;
    }
    public static ServerConfig Load(string path)
    {
        var document = TomlSerializer.Deserialize<TomlTable>(File.ReadAllText(path)) ?? new();
        var config = new ServerConfig();
        if (document.TryGetValue("server", out var value))
        {
            if (value is not TomlTable settings)
                throw new ArgumentException("server must be a TOML table");
            foreach (var (key, item) in settings)
            {
                if (key is "port" or "max_body" or "history_limit")
                {
                    if (item is not long number)
                        throw new ArgumentException($"Setting {key} in {path} must be a number");
                    if (key == "port")
                        config.Port = checked((int)number);
                    else if (key == "max_body")
                        config.MaxBody = number;
                    else
                        config.HistoryLimit = checked((int)number);
                }
                else if (key == "admin_enabled")
                {
                    if (item is not bool flag)
                        throw new ArgumentException($"Setting {key} in {path} must be a boolean");
                    config.AdminEnabled = flag;
                }
                else
                {
                    if (item is not string text)
                        throw new ArgumentException($"Setting {key} in {path} must be a string");
                    switch (key)
                    {
                        case "host":
                            config.Host = text;
                            break;
                        case "data_dir":
                            config.DataDir = text;
                            break;
                        case "token":
                            config.Token = text;
                            break;
                        case "log_level":
                            config.LogLevel = text;
                            break;
                        case "unix_socket":
                            config.UnixSocket = text;
                            break;
                        case "cors_origin":
                            config.CorsOrigin = text;
                            break;
                        default:
                            throw new ArgumentException($"Unknown setting in {path}: {key}");
                    }
                }
            }
        }
        string baseDir = Path.GetDirectoryName(Path.GetFullPath(path))!;
        config.DataDir = Path.GetFullPath(Path.Combine(baseDir, config.DataDir));
        if (!string.IsNullOrEmpty(config.UnixSocket))
            config.UnixSocket = Path.GetFullPath(Path.Combine(baseDir, config.UnixSocket));
        config.Validate();
        return config;
    }
}

public static class SiqlJson
{
    public static JsonSerializerOptions Options { get; } = Create();
    static JsonSerializerOptions Create()
    {
        var o = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
        o.Converters.Add(new BigIntConverter());
        o.Converters.Add(new BytesConverter());
        o.Converters.Add(new ComplexConverter());
        o.Converters.Add(new TupleConverter());
        o.Converters.Add(new SetConverter());
        o.Converters.Add(new DictionaryConverter());
        return o;
    }
    sealed class BigIntConverter : JsonConverter<BigInteger>
    {
        public override BigInteger Read(ref Utf8JsonReader r, Type type, JsonSerializerOptions options) => BigInteger.Parse(r.GetRawNumber()); public override void Write(Utf8JsonWriter w, BigInteger value, JsonSerializerOptions o) => w.WriteRawValue(value.ToString(System.Globalization.CultureInfo.InvariantCulture));
    }
    sealed class BytesConverter : JsonConverter<byte[]>
    {
        public override byte[] Read(ref Utf8JsonReader r, Type type, JsonSerializerOptions o) => (byte[])PythonLiteral.Read(r.GetString()!)!; public override void Write(Utf8JsonWriter w, byte[] value, JsonSerializerOptions o) => w.WriteStringValue(PythonLiteral.Write(value));
    }
    sealed class ComplexConverter : JsonConverter<Complex>
    {
        public override Complex Read(ref Utf8JsonReader r, Type t, JsonSerializerOptions o) => (Complex)PythonLiteral.Read(r.GetString()!)!; public override void Write(Utf8JsonWriter w, Complex value, JsonSerializerOptions o) => w.WriteStringValue(PythonLiteral.Write(value));
    }
    sealed class TupleConverter : JsonConverter<LiteralTuple>
    {
        public override LiteralTuple Read(ref Utf8JsonReader r, Type t, JsonSerializerOptions o) => throw new NotSupportedException(); public override void Write(Utf8JsonWriter w, LiteralTuple value, JsonSerializerOptions o)
        {
            w.WriteStartArray();
            foreach (var item in value.Items)
                JsonSerializer.Serialize(w, item, o);
            w.WriteEndArray();
        }
    }
    sealed class SetConverter : JsonConverter<HashSet<object?>>
    {
        public override HashSet<object?> Read(ref Utf8JsonReader r, Type t, JsonSerializerOptions o) => throw new NotSupportedException(); public override void Write(Utf8JsonWriter w, HashSet<object?> value, JsonSerializerOptions o) => w.WriteStringValue(PythonLiteral.Write(value));
    }
    sealed class DictionaryConverter : JsonConverter<LiteralDictionary>
    {
        public override LiteralDictionary Read(ref Utf8JsonReader r, Type t, JsonSerializerOptions o) => throw new NotSupportedException(); public override void Write(Utf8JsonWriter w, LiteralDictionary value, JsonSerializerOptions o)
        {
            w.WriteStartObject();
            foreach (System.Collections.DictionaryEntry entry in value)
            {
                string key = entry.Key switch
                {
                    null => "null",
                    bool b => b ? "true" : "false",
                    string s => s,
                    _ when Values.Numeric(entry.Key) => Convert.ToString(entry.Key, System.Globalization.CultureInfo.InvariantCulture)!,
                    _ => throw new NotSupportedException("JSON dictionary keys must be scalar")
                };
                w.WritePropertyName(key);
                JsonSerializer.Serialize(w, entry.Value, o);
            }
            w.WriteEndObject();
        }
    }
    static string GetRawNumber(this ref Utf8JsonReader r) => r.HasValueSequence ? Encoding.UTF8.GetString(r.ValueSequence.ToArray()) : Encoding.UTF8.GetString(r.ValueSpan);
}

public sealed class SiQLServer : IDisposable
{
    readonly HttpListener listener = new();
    readonly SemaphoreSlim gate = new(1, 1);
    readonly object metaLock = new();
    readonly HashSet<Task> requests = [];
    readonly Stopwatch uptime = Stopwatch.StartNew();
    readonly string startedIso = DateTimeOffset.UtcNow.ToString("O");
    readonly LinkedList<Dictionary<string, object?>> history = new();
    long requestCount, queryCount, errorCount, historySeq;

    public ServerConfig Config
    {
        get;
    }
    public Database Database
    {
        get;
    }
    public Interpreter Interpreter
    {
        get;
    }
    public Uri Address
    {
        get;
    }
    public Action<string>? Logger
    {
        get; set;
    }
    void Log(string level, string message)
    {
        int Rank(string name) => name.ToUpperInvariant() switch
        {
            "DEBUG" => 10,
            "INFO" => 20,
            "WARNING" or "WARN" => 30,
            "ERROR" => 40,
            "CRITICAL" => 50,
            _ => 0
        };
        if (Rank(level) >= Rank(Config.LogLevel))
            Logger?.Invoke($"{DateTimeOffset.Now:O} {level} {message}");
    }
    public SiQLServer(ServerConfig config)
    {
        config.Validate();
        Config = config;
        Database = new(config.DataDir);
        Interpreter = new(Database, false);
        int port = config.Port;
        if (port == 0)
        {
            var temporary = new TcpListener(IPAddress.Loopback, 0);
            temporary.Start();
            port = ((IPEndPoint)temporary.LocalEndpoint).Port;
            temporary.Stop();
        }
        string host = config.Host.Contains(':') ? "[" + config.Host + "]" : config.Host;
        Address = new($"http://{host}:{port}/");
        listener.Prefixes.Add($"http://{(config.Host is "0.0.0.0" or "::" ? "+" : host)}:{port}/");
        listener.Start();
    }
    public async Task ServeAsync(CancellationToken cancellationToken = default)
    {
        using var stop = cancellationToken.Register(() => listener.Stop());
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                var context = await listener.GetContextAsync().ConfigureAwait(false);
                Task task = Handle(context);
                lock (requests)
                    requests.Add(task);
                _ = task.ContinueWith(t => { lock (requests) requests.Remove(t); }, TaskScheduler.Default);
            }
        }
        catch (Exception e) when (cancellationToken.IsCancellationRequested && e is HttpListenerException or ObjectDisposedException) { }
        Task[] remaining;
        lock (requests)
            remaining = requests.ToArray();
        await Task.WhenAll(remaining).ConfigureAwait(false);
    }
    sealed class Reject(int status, string message, object? completed = null, double? elapsedMs = null) : Exception(message)
    {
        public int Status => status; public object? Completed => completed; public double? ElapsedMs => elapsedMs;
    }

    void RecordQuery(string query, bool ok, int statements, double elapsedMs, string? error = null)
    {
        lock (metaLock)
        {
            queryCount++;
            if (!ok) errorCount++;
            if (Config.HistoryLimit > 0)
            {
                historySeq++;
                history.AddFirst(new Dictionary<string, object?>
                {
                    ["id"] = historySeq,
                    ["query"] = query,
                    ["ok"] = ok,
                    ["statements"] = statements,
                    ["elapsed_ms"] = elapsedMs,
                    ["error"] = error,
                    ["timestamp"] = DateTimeOffset.UtcNow.ToString("O")
                });
                while (history.Count > Config.HistoryLimit)
                    history.RemoveLast();
            }
        }
    }

    List<Dictionary<string, object?>> TablesSnapshotUnlocked()
    {
        var items = new List<Dictionary<string, object?>>();
        foreach (string name in Database.Tables())
        {
            var t = Database.Table(name);
            string file = Database.File(name);
            items.Add(new Dictionary<string, object?>
            {
                ["name"] = name,
                ["rows"] = t.Rows.Count,
                ["columns"] = t.Struct.Count,
                ["bytes"] = File.Exists(file) ? new FileInfo(file).Length : 0L
            });
        }
        return items;
    }

    static Dictionary<string, object?> ProcessSnapshot()
    {
        using var proc = Process.GetCurrentProcess();
        long? rss = null, vms = null;
        int? threads = null, openFds = null;
        try { rss = proc.WorkingSet64; vms = proc.VirtualMemorySize64; threads = proc.Threads.Count; } catch { }
        if (OperatingSystem.IsLinux())
        {
            try { openFds = Directory.GetFileSystemEntries("/proc/self/fd").Length; } catch { }
        }
        return new Dictionary<string, object?>
        {
            ["pid"] = Environment.ProcessId,
            ["platform"] = OperatingSystem.IsLinux() ? "linux" : OperatingSystem.IsWindows() ? "win32" : "other",
            ["rss_bytes"] = rss,
            ["vms_bytes"] = vms,
            ["threads"] = threads,
            ["open_fds"] = openFds
        };
    }

    async Task<Dictionary<string, object?>> StatusSnapshotAsync()
    {
        List<Dictionary<string, object?>> tables;
        await gate.WaitAsync();
        try { tables = TablesSnapshotUnlocked(); }
        finally { gate.Release(); }
        long reqs, qs, errs;
        lock (metaLock) { reqs = requestCount; qs = queryCount; errs = errorCount; }
        return new Dictionary<string, object?>
        {
            ["status"] = "ok",
            ["version"] = SiqlVersion.Value,
            ["started_at"] = startedIso,
            ["uptime_seconds"] = Math.Round(uptime.Elapsed.TotalSeconds, 3),
            ["listen_address"] = Address.ToString().TrimEnd('/'),
            ["unix_socket"] = Config.UnixSocket,
            ["admin_enabled"] = Config.AdminEnabled,
            ["auth_required"] = !string.IsNullOrEmpty(Config.Token),
            ["database"] = new Dictionary<string, object?>
            {
                ["path"] = Database.Path,
                ["tables"] = tables.Count,
                ["total_rows"] = tables.Sum(t => (int)t["rows"]!),
                ["total_bytes"] = tables.Sum(t => (long)t["bytes"]!)
            },
            ["stats"] = new Dictionary<string, object?>
            {
                ["requests"] = reqs,
                ["queries"] = qs,
                ["errors"] = errs
            },
            ["process"] = ProcessSnapshot()
        };
    }

    async Task<string> MetricsTextAsync()
    {
        var status = await StatusSnapshotAsync();
        List<Dictionary<string, object?>> tables;
        await gate.WaitAsync();
        try { tables = TablesSnapshotUnlocked(); }
        finally { gate.Release(); }
        var stats = (Dictionary<string, object?>)status["stats"]!;
        var proc = (Dictionary<string, object?>)status["process"]!;
        var sb = new StringBuilder();
        sb.AppendLine("# HELP siql_uptime_seconds Server uptime in seconds");
        sb.AppendLine("# TYPE siql_uptime_seconds gauge");
        sb.AppendLine($"siql_uptime_seconds {Convert.ToString(status["uptime_seconds"], System.Globalization.CultureInfo.InvariantCulture)}");
        sb.AppendLine("# HELP siql_requests_total Total HTTP requests handled");
        sb.AppendLine("# TYPE siql_requests_total counter");
        sb.AppendLine($"siql_requests_total {stats["requests"]}");
        sb.AppendLine("# HELP siql_queries_total Total query requests processed");
        sb.AppendLine("# TYPE siql_queries_total counter");
        sb.AppendLine($"siql_queries_total {stats["queries"]}");
        sb.AppendLine("# HELP siql_query_errors_total Total failed query requests");
        sb.AppendLine("# TYPE siql_query_errors_total counter");
        sb.AppendLine($"siql_query_errors_total {stats["errors"]}");
        sb.AppendLine("# HELP siql_tables_total Number of tables in database");
        sb.AppendLine("# TYPE siql_tables_total gauge");
        sb.AppendLine($"siql_tables_total {tables.Count}");
        sb.AppendLine("# HELP siql_table_rows Number of rows per table");
        sb.AppendLine("# TYPE siql_table_rows gauge");
        foreach (var t in tables)
            sb.AppendLine($"siql_table_rows{{table=\"{t["name"]}\"}} {t["rows"]}");
        sb.AppendLine("# HELP siql_table_bytes Table file size in bytes");
        sb.AppendLine("# TYPE siql_table_bytes gauge");
        foreach (var t in tables)
            sb.AppendLine($"siql_table_bytes{{table=\"{t["name"]}\"}} {t["bytes"]}");
        if (proc["rss_bytes"] is long rss)
        {
            sb.AppendLine("# HELP siql_process_resident_memory_bytes Resident memory size in bytes");
            sb.AppendLine("# TYPE siql_process_resident_memory_bytes gauge");
            sb.AppendLine($"siql_process_resident_memory_bytes {rss}");
        }
        if (proc["open_fds"] is int fds)
        {
            sb.AppendLine("# HELP siql_process_open_fds Number of open file descriptors");
            sb.AppendLine("# TYPE siql_process_open_fds gauge");
            sb.AppendLine($"siql_process_open_fds {fds}");
        }
        return sb.ToString();
    }

    void Authorize(HttpListenerContext context)
    {
        if (string.IsNullOrEmpty(Config.Token)) return;
        string authorization = context.Request.Headers["Authorization"] ?? "";
        int space = authorization.IndexOf(' ');
        string scheme = space < 0 ? authorization : authorization[..space], given = space < 0 ? "" : authorization[(space + 1)..].Trim();
        if (!scheme.Equals("Bearer", StringComparison.OrdinalIgnoreCase) || !CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(given), Encoding.UTF8.GetBytes(Config.Token)))
            throw new Reject(401, "Missing or invalid token");
    }

    async Task Handle(HttpListenerContext context)
    {
        lock (metaLock) requestCount++;
        string remote = "";
        try { remote = context.Request.RemoteEndPoint?.ToString() ?? ""; } catch { }
        string rawUrl = context.Request.RawUrl ?? "/";
        string method = context.Request.HttpMethod;
        object? payload = null;
        int status = 200;
        string contentType = "application/json";
        byte[]? rawData = null;
        var extraHeaders = new Dictionary<string, string>();
        try
        {
            int qIdx = rawUrl.IndexOf('?');
            string route = qIdx < 0 ? rawUrl : rawUrl[..qIdx];
            string queryString = qIdx < 0 ? "" : rawUrl[(qIdx + 1)..];

            if (method == "OPTIONS")
            {
                if (!string.IsNullOrEmpty(Config.CorsOrigin))
                {
                    status = 204;
                    rawData = [];
                    contentType = "text/plain";
                    extraHeaders["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS";
                    extraHeaders["Access-Control-Allow-Headers"] = "Content-Type, Authorization";
                    extraHeaders["Access-Control-Max-Age"] = "86400";
                }
                else
                {
                    throw new Reject(405, "Method not allowed");
                }
            }
            else if (method == "GET" && route == "/")
            {
                if (!Config.AdminEnabled) throw new Reject(404, "Not found");
                status = 302;
                rawData = [];
                contentType = "text/plain";
                extraHeaders["Location"] = "/admin";
            }
            else if (method == "GET" && route is "/admin" or "/admin/")
            {
                if (!Config.AdminEnabled) throw new Reject(404, "Not found");
                rawData = Encoding.UTF8.GetBytes(AdminUi.Html);
                contentType = "text/html; charset=utf-8";
                extraHeaders["Cache-Control"] = "no-store";
                extraHeaders["X-Frame-Options"] = "SAMEORIGIN";
                extraHeaders["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'";
            }
            else if (method == "GET" && route == "/health")
            {
                payload = new Dictionary<string, object?>
                {
                    ["status"] = "ok",
                    ["version"] = SiqlVersion.Value,
                    ["uptime_seconds"] = Math.Round(uptime.Elapsed.TotalSeconds, 3)
                };
            }
            else if (method == "GET" && route == "/tables")
            {
                Authorize(context);
                await gate.WaitAsync();
                try { payload = new { tables = Database.Tables() }; }
                finally { gate.Release(); }
            }
            else if (method == "GET" && route == "/api/status")
            {
                Authorize(context);
                payload = await StatusSnapshotAsync();
            }
            else if (method == "GET" && route == "/api/tables")
            {
                Authorize(context);
                await gate.WaitAsync();
                try { payload = new { tables = TablesSnapshotUnlocked() }; }
                finally { gate.Release(); }
            }
            else if (method == "GET" && route == "/api/history")
            {
                Authorize(context);
                lock (metaLock) payload = new { history = history.ToList() };
            }
            else if (method == "DELETE" && route == "/api/history")
            {
                Authorize(context);
                lock (metaLock) history.Clear();
                payload = new { cleared = true };
            }
            else if (method == "GET" && route == "/metrics")
            {
                Authorize(context);
                rawData = Encoding.UTF8.GetBytes(await MetricsTextAsync());
                contentType = "text/plain; version=0.0.4; charset=utf-8";
            }
            else if (method == "POST" && route is "/query" or "/api/query")
            {
                Authorize(context);
                long length = context.Request.ContentLength64;
                if (length < 0)
                    throw new Reject(411, "Content-Length required");
                if (length > Config.MaxBody)
                    throw new Reject(413, "Request body too large");
                JsonDocument doc;
                try
                {
                    doc = await JsonDocument.ParseAsync(context.Request.InputStream);
                }
                catch (JsonException) { throw new Reject(400, "Body must be JSON"); }
                using (doc)
                {
                    if (doc.RootElement.ValueKind != JsonValueKind.Object || !doc.RootElement.TryGetProperty("query", out var query) || query.ValueKind != JsonValueKind.String)
                        throw new Reject(400, "Expected a JSON object with a \"query\" string");
                    string sql = query.GetString()!;
                    var completed = new List<Dictionary<string, object?>>();
                    var sw = Stopwatch.StartNew();
                    try
                    {
                        var statements = Parser.Parse(sql);
                        await gate.WaitAsync();
                        try
                        {
                            foreach (var statement in statements)
                                completed.Add(Interpreter.Run(statement).ToDictionary());
                        }
                        finally { gate.Release(); }
                    }
                    catch (QueryError e)
                    {
                        double failMs = Math.Round(sw.Elapsed.TotalMilliseconds, 3);
                        RecordQuery(sql, false, completed.Count, failMs, e.Message);
                        throw new Reject(400, e.Message, completed, failMs);
                    }
                    double okMs = Math.Round(sw.Elapsed.TotalMilliseconds, 3);
                    RecordQuery(sql, true, completed.Count, okMs);
                    payload = new Dictionary<string, object?>
                    {
                        ["results"] = completed,
                        ["elapsed_ms"] = okMs
                    };
                }
            }
            else
            {
                string[] parts = route.Split('/');
                if (method == "GET" && parts.Length == 4 && parts[1] == "api" && parts[2] == "tables" && parts[3].Length > 0)
                {
                    Authorize(context);
                    string name = Uri.UnescapeDataString(parts[3]);
                    int limit = 50, offset = 0;
                    if (!string.IsNullOrEmpty(queryString))
                    {
                        foreach (string pair in queryString.Split('&', StringSplitOptions.RemoveEmptyEntries))
                        {
                            int eq = pair.IndexOf('=');
                            string k = Uri.UnescapeDataString(eq < 0 ? pair : pair[..eq]);
                            string v = eq < 0 ? "" : Uri.UnescapeDataString(pair[(eq + 1)..]);
                            if (k == "limit" && !int.TryParse(v, out limit)) throw new Reject(400, "limit and offset must be integers");
                            if (k == "offset" && !int.TryParse(v, out offset)) throw new Reject(400, "limit and offset must be integers");
                        }
                    }
                    if (limit < 0 || limit > 1000 || offset < 0)
                        throw new Reject(400, "limit must be 0..1000 and offset >= 0");
                    await gate.WaitAsync();
                    try
                    {
                        var table = Database.Table(name);
                        string file = Database.File(name);
                        var cols = table.Struct.ColumnNames.ToList();
                        var schema = cols.Select((c, i) => new Dictionary<string, object?> { ["name"] = c, ["type"] = Values.TypeName(table.Struct.Columns[i]) }).ToList();
                        var rows = table.Rows.Skip(offset).Take(limit).Select(r => Enumerable.Range(0, cols.Count).Select(j => r.Col(j).Value).ToList()).ToList();
                        payload = new Dictionary<string, object?>
                        {
                            ["name"] = name,
                            ["columns"] = cols,
                            ["schema"] = schema,
                            ["rows"] = rows,
                            ["total_rows"] = table.Rows.Count,
                            ["limit"] = limit,
                            ["offset"] = offset,
                            ["bytes"] = File.Exists(file) ? new FileInfo(file).Length : 0L,
                            ["cache"] = table.CacheInfo
                        };
                    }
                    catch (KeyNotFoundException e) { throw new Reject(404, e.Message); }
                    catch (ArgumentException e) { throw new Reject(400, e.Message); }
                    finally { gate.Release(); }
                }
                else if (method == "POST" && parts.Length == 5 && parts[1] == "api" && parts[2] == "tables" && parts[3].Length > 0 && parts[4] == "compact")
                {
                    Authorize(context);
                    string name = Uri.UnescapeDataString(parts[3]);
                    await gate.WaitAsync();
                    try
                    {
                        var table = Database.Table(name);
                        string file = Database.File(name);
                        long before = new FileInfo(file).Length;
                        table.Compact();
                        long after = new FileInfo(file).Length;
                        payload = new { table = name, bytes_before = before, bytes_after = after };
                    }
                    catch (KeyNotFoundException e) { throw new Reject(404, e.Message); }
                    catch (ArgumentException e) { throw new Reject(400, e.Message); }
                    finally { gate.Release(); }
                }
                else if (method == "DELETE" && parts.Length == 4 && parts[1] == "api" && parts[2] == "tables" && parts[3].Length > 0)
                {
                    Authorize(context);
                    string name = Uri.UnescapeDataString(parts[3]);
                    await gate.WaitAsync();
                    try
                    {
                        Database.Drop(name);
                        payload = new { deleted = name, message = $"Dropped table {name}" };
                    }
                    catch (KeyNotFoundException e) { throw new Reject(404, e.Message); }
                    catch (ArgumentException e) { throw new Reject(400, e.Message); }
                    finally { gate.Release(); }
                }
                else
                {
                    throw new Reject(404, "Not found");
                }
            }
        }
        catch (Reject e)
        {
            status = e.Status;
            contentType = "application/json";
            rawData = null;
            var error = new Dictionary<string, object?> { ["error"] = e.Message };
            if (e.Completed is not null) error["results"] = e.Completed;
            if (e.ElapsedMs is not null) error["elapsed_ms"] = e.ElapsedMs;
            payload = error;
        }
        catch (Exception error)
        {
            Log("ERROR", error.ToString());
            status = 500;
            contentType = "application/json";
            rawData = null;
            payload = new { error = "Internal server error" };
        }
        try
        {
            byte[] data;
            if (rawData is not null)
            {
                data = rawData;
            }
            else
            {
                try
                {
                    data = JsonSerializer.SerializeToUtf8Bytes(payload, SiqlJson.Options);
                }
                catch (Exception error)
                {
                    Log("ERROR", error.ToString());
                    status = 500;
                    contentType = "application/json";
                    data = Encoding.UTF8.GetBytes("{\"error\":\"Internal server error\"}");
                }
            }
            context.Response.StatusCode = status;
            context.Response.ContentType = contentType;
            context.Response.ContentLength64 = data.Length;
            context.Response.Headers["X-Content-Type-Options"] = "nosniff";
            if (!string.IsNullOrEmpty(Config.CorsOrigin))
                context.Response.Headers["Access-Control-Allow-Origin"] = Config.CorsOrigin;
            if (status == 401)
                context.Response.Headers["WWW-Authenticate"] = "Bearer";
            foreach (var (k, v) in extraHeaders)
                context.Response.Headers[k] = v;
            if (status >= 400)
                context.Response.KeepAlive = false;
            if (data.Length > 0)
                await context.Response.OutputStream.WriteAsync(data);
            Log("INFO", $"{remote} {method} {rawUrl} {status}");
        }
        finally { context.Response.Close(); }
    }
    public void Dispose()
    {
        listener.Close();
        gate.Dispose();
    }
}

public static class Service
{
    static string Quote(string value)
    {
        value = value.Replace("%", "%%").Replace("$", "$$");
        return value.Any(c => char.IsWhiteSpace(c) || "\"'\\".Contains(c)) ? "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"" : value;
    }
    public static string SystemdUnit(string configPath, string executable = "dotnet", string assembly = "SiQL.Cli.dll", string? user = null, string description = "SiQL server", bool notify = true, int watchdogSec = 30, string? runtimeDir = "siql")
    {
        configPath = Path.GetFullPath(configPath);
        var lines = new List<string>
        {
            "[Unit]",
            $"Description={description}",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            $"Type={(notify ? "notify" : "simple")}",
            $"ExecStart={Quote(executable)} {Quote(assembly)} serve --config {Quote(configPath)}",
            "ExecReload=/bin/kill -HUP $MAINPID",
            $"WorkingDirectory={Path.GetDirectoryName(configPath)}",
            "Restart=on-failure",
            "RestartSec=5",
            "TimeoutStopSec=15",
            "# EnvironmentFile=/etc/siql/siql.env",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=full"
        };
        if (notify && watchdogSec > 0)
            lines.Add($"WatchdogSec={watchdogSec}s");
        if (!string.IsNullOrEmpty(runtimeDir))
        {
            lines.Add($"RuntimeDirectory={runtimeDir}");
            lines.Add("RuntimeDirectoryMode=0750");
        }
        if (user is not null)
            lines.Add($"User={user}");
        lines.AddRange(["", "[Install]", "WantedBy=multi-user.target", ""]);
        return string.Join('\n', lines);
    }
}
