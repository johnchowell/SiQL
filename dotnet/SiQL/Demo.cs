namespace SiQL;

public static class SiqlVersion
{
    public static string Value => typeof(Table).Assembly.GetName().Version!.ToString(3);
}

public static class Demo
{
    public static void Run(TextWriter? output = null, string? file = null)
    {
        output ??= Console.Out;
        var table = new Table(file: file);
        table.AddCols(new Dictionary<string, ColumnType> { ["name"] = ColumnType.Str, ["address"] = ColumnType.Str, ["phone"] = ColumnType.Str });
        table.Add(new Dictionary<string, object?> { ["name"] = "John Doe", ["address"] = "123 Place St.", ["phone"] = "(592) 010-2345" });
        table.Add(new Dictionary<string, object?> { ["name"] = "Jimmy Doe", ["address"] = "123 Place St. (Basement)" });
        table.AddCols(new Dictionary<string, ColumnType> { ["email"] = ColumnType.Str });
        table.EditByName("email", null, "email@placeholder.org");
        foreach (var row in table.Select("John Doe"))
            output.WriteLine(row);
        foreach (var row in table.Select("Jimmy Doe", "name"))
            output.WriteLine(row);
        output.WriteLine(table);
    }
}
