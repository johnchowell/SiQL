using System.Globalization;
using System.Numerics;
using System.Text.RegularExpressions;

namespace SiQL;

public sealed class QueryError(string message) : Exception(message);
public sealed record Token(string Kind, object? Value, int Position);
public abstract record Expr
{
    public abstract object? Eval(IReadOnlyDictionary<string, object?> row);
    public abstract IEnumerable<string> Columns
    {
        get;
    }
}
public sealed record Column(string Name) : Expr
{
    public override object? Eval(IReadOnlyDictionary<string, object?> row) => row[Name]; public override IEnumerable<string> Columns => [Name];
}
public sealed record Literal(object? Value) : Expr
{
    public override object? Eval(IReadOnlyDictionary<string, object?> row) => Value; public override IEnumerable<string> Columns => [];
}
public sealed record Compare(Expr Left, string Op, Expr Right) : Expr
{
    public override object Eval(IReadOnlyDictionary<string, object?> row) => Values.Match(Left.Eval(row), Op, Right.Eval(row)); public override IEnumerable<string> Columns => Left.Columns.Concat(Right.Columns).Distinct();
}
public sealed record And(IReadOnlyList<Expr> Items) : Expr
{
    public override object Eval(IReadOnlyDictionary<string, object?> row) => Items.All(e => Values.Truth(e.Eval(row))); public override IEnumerable<string> Columns => Items.SelectMany(e => e.Columns).Distinct();
}
public sealed record Or(IReadOnlyList<Expr> Items) : Expr
{
    public override object Eval(IReadOnlyDictionary<string, object?> row) => Items.Any(e => Values.Truth(e.Eval(row))); public override IEnumerable<string> Columns => Items.SelectMany(e => e.Columns).Distinct();
}
public sealed record Not(Expr Item) : Expr
{
    public override object Eval(IReadOnlyDictionary<string, object?> row) => !Values.Truth(Item.Eval(row)); public override IEnumerable<string> Columns => Item.Columns;
}
public sealed record IsNull(Expr Operand, bool Negate = false) : Expr
{
    public override object Eval(IReadOnlyDictionary<string, object?> row) => (Operand.Eval(row) is null) != Negate; public override IEnumerable<string> Columns => Operand.Columns;
}
public sealed record In(Expr Operand, IReadOnlyList<Expr> Items, bool Negate = false) : Expr
{
    public override object Eval(IReadOnlyDictionary<string, object?> row) => Items.Any(e => Values.Equal(Operand.Eval(row), e.Eval(row))) != Negate; public override IEnumerable<string> Columns => Operand.Columns.Concat(Items.SelectMany(e => e.Columns)).Distinct();
}
public sealed record Like(Expr Operand, string Pattern, bool Negate = false) : Expr
{
    public static bool Matches(string text, string pattern)
    {
        int[] source = Values.CodePoints(text).ToArray();
        var previous = new bool[source.Length + 1];
        previous[0] = true;
        foreach (int cp in Values.CodePoints(pattern))
        {
            var current = new bool[previous.Length];
            if (cp == '%')
                current[0] = previous[0];
            for (int i = 1; i < current.Length; i++)
                current[i] = cp == '%' ? previous[i] || current[i - 1] : previous[i - 1] && (cp == '_' || Fold(cp) == Fold(source[i - 1]));
            previous = current;
        }
        return previous[^1];
    }
    static int Fold(int cp) => cp switch { 0x130 or 0x131 => 73, 0x17f => 83, 0x212a => 75, 0xdf or 0x1e9e => 0xdf, >= 0xd800 and <= 0xdfff => cp, _ => System.Text.Rune.ToUpperInvariant(new System.Text.Rune(cp)).Value };
    public override object Eval(IReadOnlyDictionary<string, object?> row) => Operand.Eval(row) is string s && Matches(s, Pattern) != Negate;
    public override IEnumerable<string> Columns => Operand.Columns;
}
public abstract record Statement;
public sealed record CreateTable(string Table, IReadOnlyDictionary<string, ColumnType> Columns, bool IfNotExists = false) : Statement;
public sealed record DropTable(string Table, bool IfExists = false) : Statement;
public sealed record AddColumn(string Table, string Column, ColumnType Type) : Statement;
public sealed record DropColumn(string Table, string Column) : Statement;
public sealed record AlterColumn(string Table, string Column, ColumnType Type) : Statement;
public sealed record RenameColumn(string Table, string Column, string NewName) : Statement;
public sealed record Insert(string Table, IReadOnlyList<string>? Columns, IReadOnlyList<IReadOnlyList<object?>> Rows) : Statement;
public sealed record Select(string Table, IReadOnlyList<string>? Columns, bool Count = false, Expr? Where = null,
    IReadOnlyList<(string Column, bool Descending)>? Order = null, int? Limit = null, int Offset = 0) : Statement;
public sealed record Update(string Table, IReadOnlyDictionary<string, object?> Values, Expr? Where = null) : Statement;
public sealed record Delete(string Table, Expr? Where = null) : Statement;
public sealed record ShowTables : Statement;
public sealed record ShowFiles : Statement;
public sealed record Describe(string Table) : Statement;
public sealed record RenameTable(string Table, string NewName) : Statement;
public sealed record CopyTable(string Table, string NewName) : Statement;
public sealed record Truncate(string Table) : Statement;
public sealed record Compact(string? Table = null) : Statement;
public sealed record Use(string Path) : Statement;
public sealed record Import(string Path, string Table) : Statement;
public sealed record Export(string Table, string Path) : Statement;

public sealed class Parser
{
    static readonly HashSet<string> reserved = "SELECT FROM WHERE AND OR NOT ORDER BY LIMIT OFFSET INSERT INTO VALUES UPDATE SET DELETE CREATE DROP ALTER TABLE IS NULL NONE IN LIKE ASC DESC TRUE FALSE SHOW DESCRIBE".Split().ToHashSet();
    static readonly HashSet<string> types = "str text string varchar int integer float real double bool boolean list dict object any".Split().ToHashSet();
    static readonly Regex token = new(@"\G(?:(?<ws>\s+|--[^\n]*)|(?<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)|(?<str>'(?:[^']|'')*'|""(?:[^""]|"""")*"")|(?<qname>`(?:[^`]|``)+`)|(?<name>[A-Za-z_][A-Za-z0-9_]*)|(?<op><=|>=|!=|<>|==|[=<>(),*;\-]))", RegexOptions.CultureInvariant);
    public static IReadOnlyList<Token> Tokenize(string text)
    {
        var result = new List<Token>();
        int pos = 0;
        while (pos < text.Length)
        {
            var m = token.Match(text, pos);
            if (!m.Success)
                throw new QueryError($"Unexpected character {text[pos]} at position {pos}");
            string kind = new[] { "ws", "num", "str", "qname", "name", "op" }.First(k => m.Groups[k].Success);
            string raw = m.Value;
            object value = raw;
            if (kind == "num")
            {
                if (raw.IndexOfAny(['.', 'e', 'E']) >= 0)
                    value = double.Parse(raw, CultureInfo.InvariantCulture);
                else
                {
                    var n = BigInteger.Parse(raw, CultureInfo.InvariantCulture);
                    value = n >= long.MinValue && n <= long.MaxValue ? (object)(long)n : n;
                }
            }
            else if (kind == "str")
                value = raw[1..^1].Replace(new string(raw[0], 2), raw[..1]);
            else if (kind == "qname")
                value = raw[1..^1].Replace("``", "`");
            if (kind != "ws")
                result.Add(new(kind, value, pos));
            pos = m.Index + m.Length;
        }
        result.Add(new("end", null, pos));
        return result;
    }
    readonly IReadOnlyList<Token> tokens; int position;
    public Parser(string text) => tokens = Tokenize(text);
    Token Current => tokens[position]; Token Advance() => tokens[position++];
    bool Is(params string[] words) => Current.Kind == "name" && words.Contains(((string)Current.Value!).ToUpperInvariant());
    string? Kw(params string[] words)
    {
        if (!Is(words))
            return null;
        return ((string)Advance().Value!).ToUpperInvariant();
    }
    string Expect(params string[] words) => Kw(words) ?? throw Error("Expected " + string.Join(" or ", words));
    string? Op(params string[] ops)
    {
        if (Current.Kind != "op" || !ops.Contains((string)Current.Value!))
            return null;
        return (string)Advance().Value!;
    }
    string Need(params string[] ops) => Op(ops) ?? throw Error("Expected " + string.Join(" or ", ops));
    QueryError Error(string message) => new($"{message} at position {Current.Position}");
    string Ident()
    {
        if (Current.Kind == "qname" || Current.Kind == "name" && !reserved.Contains(((string)Current.Value!).ToUpperInvariant()))
            return (string)Advance().Value!;
        throw Error("Expected a name");
    }
    string Path()
    {
        if (Current.Kind != "str")
            throw Error("Expected a quoted file path");
        return (string)Advance().Value!;
    }
    ColumnType Type()
    {
        if (Current.Kind != "name" || !types.Contains(((string)Current.Value!).ToLowerInvariant()))
            throw Error("Expected a column type");
        return Values.TypeFromName((string)Advance().Value!);
    }
    object? Value()
    {
        if (Current.Kind is "str" or "num")
            return Advance().Value;
        if (Op("-") is not null)
        {
            if (Current.Kind != "num")
                throw Error("Expected a number");
            var v = Advance().Value!;
            return v is double d ? -d : v is BigInteger bi ? -bi : -(long)v;
        }
        string? kw = Kw("NULL", "NONE", "TRUE", "FALSE");
        return kw switch
        {
            "NULL" or "NONE" => null,
            "TRUE" => true,
            "FALSE" => false,
            _ => throw Error("Expected a value")
        };
    }
    int Whole()
    {
        if (Current.Kind != "num" || Current.Value is double)
            throw Error("Expected a whole number");
        return checked((int)BigInteger.Parse(Advance().Value!.ToString()!));
    }
    List<string> Names()
    {
        var names = new List<string> { Ident() };
        while (Op(",") is not null)
            names.Add(Ident());
        return names;
    }
    public static IReadOnlyList<Statement> Parse(string text) => new Parser(text).Script();
    public IReadOnlyList<Statement> Script()
    {
        var result = new List<Statement>();
        while (Current.Kind != "end")
        {
            if (Op(";") is not null)
                continue;
            result.Add(Statement());
            if (Current.Kind != "end")
                Need(";");
        }
        return result;
    }
    public Statement Statement()
    {
        string kind = Expect("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "SHOW", "DESCRIBE", "RENAME", "COPY", "TRUNCATE", "COMPACT", "USE", "IMPORT", "EXPORT");
        switch (kind)
        {
            case "CREATE":
                {
                    Expect("TABLE");
                    bool optional = Kw("IF") is not null;
                    if (optional)
                    {
                        Expect("NOT");
                        Expect("EXISTS");
                    }
                    string name = Ident();
                    Need("(");
                    var cols = new Dictionary<string, ColumnType>();
                    do
                    {
                        string c = Ident();
                        if (cols.ContainsKey(c))
                            throw new QueryError($"Duplicate column: {c}");
                        cols[c] = Type();
                    } while (Op(",") is not null);
                    Need(")");
                    return new CreateTable(name, cols, optional);
                }
            case "DROP":
                {
                    Expect("TABLE");
                    bool optional = Kw("IF") is not null;
                    if (optional)
                        Expect("EXISTS");
                    return new DropTable(Ident(), optional);
                }
            case "ALTER":
                {
                    Expect("TABLE");
                    string name = Ident(), action = Expect("ADD", "DROP", "ALTER", "RENAME");
                    Kw("COLUMN");
                    string col = Ident();
                    if (action == "ADD")
                        return new AddColumn(name, col, Type());
                    if (action == "DROP")
                        return new DropColumn(name, col);
                    if (action == "RENAME")
                    {
                        Expect("TO");
                        return new RenameColumn(name, col, Ident());
                    }
                    Kw("TYPE");
                    return new AlterColumn(name, col, Type());
                }
            case "INSERT":
                {
                    Expect("INTO");
                    string name = Ident();
                    List<string>? cols = null;
                    if (Op("(") is not null)
                    {
                        cols = Names();
                        Need(")");
                    }
                    Expect("VALUES");
                    var rows = new List<IReadOnlyList<object?>>();
                    do
                    {
                        Need("(");
                        var row = new List<object?> { Value() };
                        while (Op(",") is not null)
                            row.Add(Value());
                        Need(")");
                        rows.Add(row);
                    } while (Op(",") is not null);
                    return new Insert(name, cols, rows);
                }
            case "SELECT":
                {
                    bool count = false;
                    List<string>? cols = null;
                    if (Is("COUNT") && tokens[position + 1].Value is "(")
                    {
                        Advance();
                        Advance();
                        Need("*");
                        Need(")");
                        count = true;
                    }
                    else if (Op("*") is null)
                        cols = Names();
                    Expect("FROM");
                    string name = Ident();
                    Expr? where = Where();
                    var order = new List<(string, bool)>();
                    if (Kw("ORDER") is not null)
                    {
                        Expect("BY");
                        do
                        {
                            string col = Ident();
                            order.Add((col, Kw("ASC", "DESC") == "DESC"));
                        } while (Op(",") is not null);
                    }
                    int? limit = null;
                    int offset = 0;
                    if (Kw("LIMIT") is not null)
                    {
                        limit = Whole();
                        if (Kw("OFFSET") is not null)
                            offset = Whole();
                    }
                    return new Select(name, cols, count, where, order, limit, offset);
                }
            case "UPDATE":
                {
                    string name = Ident();
                    Expect("SET");
                    var vals = new Dictionary<string, object?>();
                    do
                    {
                        string col = Ident();
                        Need("=", "==");
                        vals[col] = Value();
                    } while (Op(",") is not null);
                    return new Update(name, vals, Where());
                }
            case "DELETE":
                Expect("FROM");
                return new Delete(Ident(), Where());
            case "SHOW":
                return Expect("TABLES", "FILES") == "FILES" ? new ShowFiles() : new ShowTables();
            case "DESCRIBE":
                return new Describe(Ident());
            case "RENAME":
            case "COPY":
                {
                    Expect("TABLE");
                    string name = Ident();
                    Expect("TO");
                    string next = Ident();
                    return kind == "RENAME" ? new RenameTable(name, next) : new CopyTable(name, next);
                }
            case "TRUNCATE":
                Kw("TABLE");
                return new Truncate(Ident());
            case "COMPACT":
                return Kw("TABLE") is not null || Current.Kind is "name" or "qname" ? new Compact(Ident()) : new Compact();
            case "USE":
                return new Use(Path());
            case "IMPORT":
                {
                    string path = Path();
                    Expect("INTO");
                    return new Import(path, Ident());
                }
            case "EXPORT":
                {
                    Kw("TABLE");
                    string name = Ident();
                    Expect("TO");
                    return new Export(name, Path());
                }
            default:
                throw Error("Unsupported statement");
        }
    }
    Expr? Where() => Kw("WHERE") is null ? null : Expression();
    public Expr Expression()
    {
        var items = new List<Expr> { AndExpression() };
        while (Kw("OR") is not null)
            items.Add(AndExpression());
        return items.Count == 1 ? items[0] : new Or(items);
    }
    Expr AndExpression()
    {
        var items = new List<Expr> { NotExpression() };
        while (Kw("AND") is not null)
            items.Add(NotExpression());
        return items.Count == 1 ? items[0] : new And(items);
    }
    Expr NotExpression() => Kw("NOT") is not null ? new Not(NotExpression()) : Predicate();
    Expr Operand() => Current.Kind == "qname" || Current.Kind == "name" && !reserved.Contains(((string)Current.Value!).ToUpperInvariant()) ? new Column((string)Advance().Value!) : new Literal(Value());
    Expr Predicate()
    {
        if (Op("(") is not null)
        {
            var e = Expression();
            Need(")");
            return e;
        }
        Expr left = Operand();
        if (Kw("IS") is not null)
        {
            bool neg = Kw("NOT") is not null;
            Expect("NULL", "NONE");
            return new IsNull(left, neg);
        }
        bool negate = Kw("NOT") is not null;
        if (Kw("IN") is not null)
        {
            Need("(");
            var items = new List<Expr> { Operand() };
            while (Op(",") is not null)
                items.Add(Operand());
            Need(")");
            return new In(left, items, negate);
        }
        if (Kw("LIKE") is not null)
            return new Like(left, Path(), negate);
        if (negate)
            throw Error("Expected IN or LIKE");
        return new Compare(left, Need("=", "==", "!=", "<>", "<", ">", "<=", ">="), Operand());
    }
}
