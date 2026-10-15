using SiQL;

return await Cli.Run(args);

static class Cli
{
    public static async Task<int> Run(string[] args)
    {
        try
        {
            var words = args.ToList();
            string? Take(string shortName, string longName, string? fallback = null)
            {
                int i = words.FindIndex(w => w == shortName || w == longName);
                if (i < 0)
                    return fallback;
                if (i + 1 == words.Count)
                    throw new ArgumentException($"Missing value for {words[i]}");
                string value = words[i + 1];
                words.RemoveRange(i, 2);
                return value;
            }
            bool Flag(string name)
            {
                int i = words.IndexOf(name);
                if (i < 0)
                    return false;
                words.RemoveAt(i);
                return true;
            }
            if (Flag("--help") || Flag("-h"))
            {
                Console.WriteLine("siql-dotnet [-d DIR] [-f FILE] [SQL ...]\nsiql-dotnet serve [-c CONFIG] [--host HOST] [--port PORT] [--data-dir DIR]\nsiql-dotnet init [-c CONFIG] [--force]\nsiql-dotnet systemd [-c CONFIG] [--user USER] [-o FILE]");
                return 0;
            }
            string? verb = words.Count > 0 && words[0] is "serve" or "init" or "systemd" or "demo" ? words[0] : null;
            if (verb is not null)
                words.RemoveAt(0);
            if (verb is "serve" or "init" or "systemd")
            {
                string? path = Take("-c", "--config", verb == "serve" ? null : "siql.toml");
                if (verb == "init")
                {
                    bool force = Flag("--force");
                    using var stream = new FileStream(path!, force ? FileMode.Create : FileMode.CreateNew, FileAccess.Write);
                    using var writer = new StreamWriter(stream);
                    writer.Write(ServerConfig.Template);
                    Console.WriteLine($"Wrote {path}");
                    return 0;
                }
                if (verb == "systemd")
                {
                    if (!File.Exists(path))
                        throw new ArgumentException($"No config file at {path}; create one with: siql-dotnet init");
                    string? user = Take("--user", "--user"), output = Take("-o", "--output");
                    string unit = Service.SystemdUnit(path!, assembly: typeof(Cli).Assembly.Location, user: user);
                    if (output is null)
                        Console.Write(unit);
                    else
                        File.WriteAllText(output, unit);
                    return 0;
                }
                var config = path is null ? new ServerConfig() : ServerConfig.Load(path);
                config.Host = Take("--host", "--host", config.Host)!;
                config.DataDir = Take("-d", "--data-dir", config.DataDir)!;
                if (Take("--port", "--port") is { } port)
                    config.Port = int.Parse(port);
                config.Token = Environment.GetEnvironmentVariable("SIQL_TOKEN") is { Length: > 0 } token ? token : config.Token;
                config.Validate();
                if (!new[] { "DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL", "FATAL", "NOTSET" }.Contains(config.LogLevel.ToUpperInvariant()))
                    throw new ArgumentException($"Unknown log level: {config.LogLevel}");
                using var server = new SiQLServer(config) { Logger = Console.Error.WriteLine };
                using var stop = new CancellationTokenSource();
                Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Cancel(); };
                Console.WriteLine($"SiQL server listening on {server.Address} (data: {server.Database.Path})");
                await server.ServeAsync(stop.Token);
                return 0;
            }
            string directory = Take("-d", "--data-dir", ".")!, file = Take("-f", "--file", "")!;
            var interpreter = new Interpreter(directory);
            if (verb == "demo")
            {
                Demo.Run(file: Path.Combine(directory, Guid.NewGuid().ToString("N") + ".siql"));
                return 0;
            }
            bool Execute(string text)
            {
                try
                {
                    foreach (var statement in Parser.Parse(text))
                        Console.WriteLine(interpreter.Run(statement));
                    return true;
                }
                catch (QueryError e) { Console.Error.WriteLine("Error: " + e.Message); return false; }
            }
            if (file != "" || words.Count != 0)
            {
                if (file != "" && !Execute(File.ReadAllText(file)))
                    return 1;
                return Execute(string.Join(' ', words)) ? 0 : 1;
            }
            if (Console.IsInputRedirected)
                return Execute(Console.In.ReadToEnd()) ? 0 : 1;
            Console.WriteLine($"SiQL shell on {interpreter.Database.Path}. End statements with ';'. Type .exit to quit.");
            var buffer = new List<string>();
            while (true)
            {
                Console.Write(buffer.Count == 0 ? "siql> " : "  ... ");
                string? line = Console.ReadLine();
                if (line is null)
                    return 0;
                if (buffer.Count == 0 && line.Trim() is ".exit" or ".quit")
                    return 0;
                buffer.Add(line);
                if (line.TrimEnd().EndsWith(';'))
                {
                    Execute(string.Join('\n', buffer));
                    buffer.Clear();
                }
            }
        }
        catch (Exception e) when (e is QueryError or IOException or ArgumentException or FormatException) { Console.Error.WriteLine("Error: " + e.Message); return 1; }
    }
}
