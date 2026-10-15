using System.Text.RegularExpressions;

namespace SiQL;

public sealed class Database
{
    readonly Dictionary<string, Table> open = new(OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
    public string Path
    {
        get;
    }
    public Database(string path = ".")
    {
        Path = System.IO.Path.GetFullPath(path);
        Directory.CreateDirectory(Path);
    }
    public string File(string name)
    {
        if (!Regex.IsMatch(name, @"\A[A-Za-z_][A-Za-z0-9_]*\z"))
            throw new ArgumentException($"Invalid table name: {name}");
        return System.IO.Path.Combine(Path, name + ".siql");
    }
    public IReadOnlyList<string> Tables() => Directory.EnumerateFiles(Path, "*.siql").Select(System.IO.Path.GetFileNameWithoutExtension).Where(n => n is not null && Regex.IsMatch(n, @"\A[A-Za-z_][A-Za-z0-9_]*\z")).Select(n => n!).Order(StringComparer.Ordinal).ToArray();
    public bool Contains(string name)
    {
        try
        {
            return System.IO.File.Exists(File(name));
        }
        catch (ArgumentException) { return false; }
    }
    public Table Table(string name)
    {
        string file = File(name);
        if (!open.TryGetValue(file, out var table))
        {
            if (!System.IO.File.Exists(file))
                throw new KeyNotFoundException($"No such table: {name}");
            open[file] = table = new(file: file);
        }
        return table;
    }
    public Table Create(string name, IReadOnlyDictionary<string, ColumnType>? columns = null)
    {
        string file = File(name);
        if (System.IO.File.Exists(file))
            throw new ArgumentException($"Table already exists: {name}");
        var table = new Table(file: file);
        if (columns is not null)
            table.AddCols(columns);
        open[file] = table;
        return table;
    }
    public void Drop(string name)
    {
        string file = File(name);
        if (!System.IO.File.Exists(file))
            throw new KeyNotFoundException($"No such table: {name}");
        open.Remove(file);
        System.IO.File.Delete(file);
    }
    void Check(string old, string next)
    {
        if (!Contains(old))
            throw new KeyNotFoundException($"No such table: {old}");
        if (System.IO.File.Exists(File(next)))
            throw new ArgumentException($"Table already exists: {next}");
    }
    public Table Rename(string name, string next)
    {
        Check(name, next);
        string old = File(name), file = File(next);
        System.IO.File.Move(old, file);
        if (open.Remove(old, out var table))
        {
            table.FileName = file;
            open[file] = table;
            return table;
        }
        return Table(next);
    }
    public Table Copy(string name, string next)
    {
        Check(name, next);
        System.IO.File.Copy(File(name), File(next));
        return Table(next);
    }
}
