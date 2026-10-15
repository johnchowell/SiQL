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
    public const string Template = """
        # SiQL server configuration
        [server]
        host = "127.0.0.1"
        port = 8642
        data_dir = "data"
        # token = "..."  # SIQL_TOKEN overrides this in the CLI
        max_body = 1048576
        log_level = "INFO"
        """ + "\n";
    public void Validate()
    {
        if (Port < 0 || Port > 65535)
            throw new ArgumentException($"Invalid port: {Port}");
        if (MaxBody < 0)
            throw new ArgumentException($"Invalid max_body: {MaxBody}");
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
                if (key is "port" or "max_body")
                {
                    if (item is not long number)
                        throw new ArgumentException($"Setting {key} in {path} must be a number");
                    if (key == "port")
                        config.Port = checked((int)number);
                    else
                        config.MaxBody = number;
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
                        default:
                            throw new ArgumentException($"Unknown setting in {path}: {key}");
                    }
                }
            }
        }
        config.DataDir = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(path))!, config.DataDir));
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
    readonly HashSet<Task> requests = [];
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
    sealed class Reject(int status, string message, object? completed = null) : Exception(message)
    {
        public int Status => status; public object? Completed => completed;
    }
    async Task Handle(HttpListenerContext context)
    {
        object payload;
        int status = 200;
        try
        {
            string route = (context.Request.RawUrl ?? "/").Split('?', 2)[0], method = context.Request.HttpMethod;
            bool health = method == "GET" && route == "/health";
            if (!health && !(method == "GET" && route == "/tables") && !(method == "POST" && route == "/query"))
                throw new Reject(404, "Not found");
            if (!health && !string.IsNullOrEmpty(Config.Token))
            {
                string authorization = context.Request.Headers["Authorization"] ?? "";
                int space = authorization.IndexOf(' ');
                string scheme = space < 0 ? authorization : authorization[..space], given = space < 0 ? "" : authorization[(space + 1)..].Trim();
                if (!scheme.Equals("Bearer", StringComparison.OrdinalIgnoreCase) || !CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(given), Encoding.UTF8.GetBytes(Config.Token)))
                    throw new Reject(401, "Missing or invalid token");
            }
            if (health)
                payload = new
                {
                    status = "ok",
                    version = SiqlVersion.Value
                };
            else if (route == "/tables")
            {
                await gate.WaitAsync();
                try
                {
                    payload = new
                    {
                        tables = Database.Tables()
                    };
                }
                finally { gate.Release(); }
            }
            else
            {
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
                    var completed = new List<Dictionary<string, object?>>();
                    try
                    {
                        var statements = Parser.Parse(query.GetString()!);
                        await gate.WaitAsync();
                        try
                        {
                            foreach (var statement in statements)
                                completed.Add(Interpreter.Run(statement).ToDictionary());
                        }
                        finally { gate.Release(); }
                    }
                    catch (QueryError e) { throw new Reject(400, e.Message, completed); }
                    payload = new
                    {
                        results = completed
                    };
                }
            }
        }
        catch (Reject e) { status = e.Status; var error = new Dictionary<string, object?> { ["error"] = e.Message }; if (e.Completed is not null) error["results"] = e.Completed; payload = error; }
        catch (Exception error) { Log("ERROR", error.ToString()); status = 500; payload = new { error = "Internal server error" }; }
        try
        {
            byte[] data;
            try
            {
                data = JsonSerializer.SerializeToUtf8Bytes(payload, SiqlJson.Options);
            }
            catch (Exception error) { Log("ERROR", error.ToString()); status = 500; data = Encoding.UTF8.GetBytes("{\"error\":\"Internal server error\"}"); }
            context.Response.StatusCode = status;
            context.Response.ContentType = "application/json";
            context.Response.ContentLength64 = data.Length;
            if (status == 401)
                context.Response.Headers["WWW-Authenticate"] = "Bearer";
            if (status >= 400)
                context.Response.KeepAlive = false;
            await context.Response.OutputStream.WriteAsync(data);
            Log("INFO", $"{context.Request.RemoteEndPoint} {context.Request.HttpMethod} {context.Request.RawUrl} {status}");
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
    public static string SystemdUnit(string configPath, string executable = "dotnet", string assembly = "SiQL.Cli.dll", string? user = null, string description = "SiQL server")
    {
        configPath = Path.GetFullPath(configPath);
        var lines = new List<string> { "[Unit]", $"Description={description}", "After=network-online.target", "Wants=network-online.target", "", "[Service]", "Type=simple", $"ExecStart={Quote(executable)} {Quote(assembly)} serve --config {Quote(configPath)}", $"WorkingDirectory={Path.GetDirectoryName(configPath)}", "Restart=on-failure", "# EnvironmentFile=/etc/siql/siql.env", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=full" };
        if (user is not null)
            lines.Add($"User={user}");
        lines.AddRange(["", "[Install]", "WantedBy=multi-user.target", ""]);
        return string.Join('\n', lines);
    }
}
