namespace SiQL;

public enum ScanOp
{
    Eq, Ne, Lt, Le, Gt, Ge, IsNull, NotNull
}
public static class Search
{
    public static IReadOnlyList<int> Scan(IReadOnlyList<Cell> cells, ScanOp op, object? value = null, bool valueFirst = false)
    {
        var result = new List<int>();
        string[] comparisons = ["=", "!=", "<", "<=", ">", ">="];
        for (int i = 0; i < cells.Count; i++)
        {
            object? found = cells[i].Value;
            bool match = op == ScanOp.IsNull ? found is null : op == ScanOp.NotNull ? found is not null : valueFirst ? Values.Match(value, comparisons[(int)op], found) : Values.Match(found, comparisons[(int)op], value);
            if (match)
                result.Add(i);
        }
        return result;
    }
    public static IReadOnlyList<int> ScanMatch(IReadOnlyList<Cell> cells, Func<string, bool> fullmatch, bool negate = false) => cells.Select((cell, i) => (cell, i)).Where(p => p.cell.Value is string s && fullmatch(s) != negate).Select(p => p.i).ToArray();
    public static IReadOnlyList<object?> ValuesOf(IEnumerable<Cell> cells) => cells.Select(c => c.Value).ToArray();
}
