using System.Collections;
using System.Globalization;
using System.Numerics;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.RegularExpressions;

namespace SiQL;

public enum ColumnType
{
    Str, Int, Float, Bool, List, Dict, Any, Bytes, Tuple, Set, NoneType, Complex
}

public static class Values
{
    public static IEnumerable<DictionaryEntry> DictionaryEntries(IDictionary dictionary)
    {
        var e = dictionary.GetEnumerator();
        while (e.MoveNext())
            yield return e.Entry;
    }
    public static ColumnType TypeFromName(string name) => name.ToLowerInvariant() switch
    {
        "str" or "text" or "string" or "varchar" => ColumnType.Str,
        "int" or "integer" => ColumnType.Int,
        "float" or "real" or "double" => ColumnType.Float,
        "bool" or "boolean" => ColumnType.Bool,
        "list" => ColumnType.List,
        "dict" => ColumnType.Dict,
        "bytes" => ColumnType.Bytes,
        "tuple" => ColumnType.Tuple,
        "set" => ColumnType.Set,
        "nonetype" => ColumnType.NoneType,
        "complex" => ColumnType.Complex,
        _ => ColumnType.Any
    };
    public static string TypeName(ColumnType type) => type == ColumnType.Any ? "object" : type == ColumnType.NoneType ? "NoneType" : type.ToString().ToLowerInvariant();
    public static bool Numeric(object? value) => value is bool or byte or sbyte or short or ushort or int or uint or long or ulong or BigInteger or float or double;
    public static (BigInteger N, BigInteger D) Ratio(object value)
    {
        if (value is bool b)
            return (b ? 1 : 0, 1);
        if (value is BigInteger bi)
            return (bi, 1);
        if (value is not double and not float)
            return (BigInteger.Parse(Convert.ToString(value, CultureInfo.InvariantCulture)!), 1);
        double d = Convert.ToDouble(value, CultureInfo.InvariantCulture);
        if (!double.IsFinite(d))
            throw new ArgumentException("Nonfinite number has no rational encoding");
        long bits = BitConverter.DoubleToInt64Bits(d);
        int exp = (int)((bits >> 52) & 2047);
        BigInteger n = bits & 0xfffffffffffffL;
        if (exp != 0)
            n |= BigInteger.One << 52;
        int shift = (exp == 0 ? -1022 : exp - 1023) - 52;
        BigInteger den = BigInteger.One;
        if (shift >= 0)
            n <<= shift;
        else
            den <<= -shift;
        if (bits < 0)
            n = -n;
        var gcd = BigInteger.GreatestCommonDivisor(n, den);
        return (n / gcd, den / gcd);
    }
    public static bool Equal(object? left, object? right)
    {
        static bool Builtin(object value) => Numeric(value) || value is string or byte[] or Complex or LiteralTuple or IList or IDictionary or HashSet<object?>;
        if (left is not null && !Builtin(left))
            return left.Equals(right);
        if (right is not null && !Builtin(right))
            return right.Equals(left);
        if (left is Complex lc)
        {
            if (right is Complex rc)
                return Equal(lc.Real, rc.Real) && Equal(lc.Imaginary, rc.Imaginary);
            return lc.Imaginary == 0 && Numeric(right) && Equal(lc.Real, right);
        }
        if (right is Complex rc2)
            return rc2.Imaginary == 0 && Numeric(left) && Equal(left, rc2.Real);
        if (Numeric(left) && Numeric(right))
        {
            if (left is double ld && !double.IsFinite(ld) || right is double rd && !double.IsFinite(rd) ||
                left is float lf && !float.IsFinite(lf) || right is float rf && !float.IsFinite(rf))
                return Convert.ToDouble(left) == Convert.ToDouble(right);
            var a = Ratio(left!);
            var b = Ratio(right!);
            return a.N * b.D == b.N * a.D;
        }
        if (left is null || right is null)
            return left is null && right is null;
        if (left is byte[] lb && right is byte[] rb)
            return lb.AsSpan().SequenceEqual(rb);
        if (left is IDictionary lm && right is IDictionary rm)
        {
            if (lm.Count != rm.Count)
                return false;
            foreach (DictionaryEntry entry in lm)
            {
                bool found = false;
                foreach (DictionaryEntry other in rm)
                    if (Equal(entry.Key, other.Key) && Equal(entry.Value, other.Value))
                    {
                        found = true;
                        break;
                    }
                if (!found)
                    return false;
            }
            return true;
        }
        if (left is LiteralTuple lt && right is LiteralTuple rt)
            return SequenceEqual(lt.Items, rt.Items);
        if (left is IList ll && right is IList rl)
            return SequenceEqual(ll.Cast<object?>(), rl.Cast<object?>());
        if (left is HashSet<object?> ls && right is HashSet<object?> rs)
            return ls.Count == rs.Count && ls.All(a => rs.Any(b => Equal(a, b)));
        return left.Equals(right);
    }
    static bool SequenceEqual(IEnumerable<object?> a, IEnumerable<object?> b) => a.Zip(b, Equal).All(x => x) && a.Count() == b.Count();
    public static int Compare(object? left, object? right)
    {
        if (Numeric(left) && Numeric(right))
        {
            double? ld = left is double or float ? Convert.ToDouble(left) : null, rd = right is double or float ? Convert.ToDouble(right) : null;
            if (ld is { } ln && double.IsNaN(ln) || rd is { } rn && double.IsNaN(rn))
                throw new ArgumentException("NaN cannot be ordered");
            if (ld is { } li && double.IsInfinity(li))
                return rd is { } ri && double.IsInfinity(ri) ? li.CompareTo(ri) : Math.Sign(li);
            if (rd is { } rinf && double.IsInfinity(rinf))
                return -Math.Sign(rinf);
            var a = Ratio(left!);
            var b = Ratio(right!);
            return (a.N * b.D).CompareTo(b.N * a.D);
        }
        if (left is string l && right is string r)
        {
            var a = CodePoints(l).GetEnumerator();
            var b = CodePoints(r).GetEnumerator();
            while (true)
            {
                bool x = a.MoveNext(), y = b.MoveNext();
                if (!x || !y)
                    return x.CompareTo(y);
                int c = a.Current.CompareTo(b.Current);
                if (c != 0)
                    return c;
            }
        }
        if (left is byte[] lb && right is byte[] rb)
            return lb.AsSpan().SequenceCompareTo(rb);
        if (left is IList ll && right is IList rl)
        {
            for (int i = 0; i < Math.Min(ll.Count, rl.Count); i++)
            {
                if (!Equal(ll[i], rl[i]))
                    return Compare(ll[i], rl[i]);
            }
            return ll.Count.CompareTo(rl.Count);
        }
        throw new ArgumentException("Values cannot be ordered");
    }
    public static IEnumerable<int> CodePoints(string text)
    {
        for (int i = 0; i < text.Length; i++)
        {
            if (char.IsHighSurrogate(text[i]) && i + 1 < text.Length && char.IsLowSurrogate(text[i + 1]))
                yield return char.ConvertToUtf32(text[i], text[++i]);
            else
                yield return text[i];
        }
    }
    public static byte[] Utf8SurrogatePass(string text)
    {
        var bytes = new List<byte>();
        foreach (int cp in CodePoints(text))
        {
            if (cp < 128)
                bytes.Add((byte)cp);
            else if (cp < 2048)
            {
                bytes.Add((byte)(192 | (cp >> 6)));
                bytes.Add((byte)(128 | (cp & 63)));
            }
            else if (cp < 65536)
            {
                bytes.Add((byte)(224 | (cp >> 12)));
                bytes.Add((byte)(128 | ((cp >> 6) & 63)));
                bytes.Add((byte)(128 | (cp & 63)));
            }
            else
            {
                bytes.Add((byte)(240 | (cp >> 18)));
                bytes.Add((byte)(128 | ((cp >> 12) & 63)));
                bytes.Add((byte)(128 | ((cp >> 6) & 63)));
                bytes.Add((byte)(128 | (cp & 63)));
            }
        }
        return bytes.ToArray();
    }
    public static string? TreeKey(object? value)
    {
        if (value is string text)
            return text.Length == 0 ? "" : char.IsHighSurrogate(text[0]) && text.Length > 1 && char.IsLowSurrogate(text[1]) ? text[..2] : text[..1];
        if (Numeric(value))
        {
            try
            {
                var r = Ratio(value!);
                return BigInteger.Abs(r.N / r.D).ToString(CultureInfo.InvariantCulture)[..1];
            }
            catch (ArgumentException) { }
        }
        return null;
    }
    public static bool Cacheable(object? value) => value is null or string || Numeric(value) && !(value is double d && double.IsNaN(d)) && !(value is float f && float.IsNaN(f));
    public static string CacheKey(object? value)
    {
        if (value is null)
            return "null";
        if (value is string s)
            return "s:" + s;
        if (value is double d && double.IsInfinity(d))
            return d > 0 ? "+inf" : "-inf";
        if (value is float f && float.IsInfinity(f))
            return f > 0 ? "+inf" : "-inf";
        var r = Ratio(value!);
        return FormattableString.Invariant($"n:{r.N}/{r.D}");
    }
    public static bool Truth(object? value) => value switch { null => false, bool b => b, string s => s.Length != 0, ICollection c => c.Count != 0, _ when Numeric(value) => !Equal(value, 0), _ => true };
    public static bool Match(object? left, string op, object? right)
    {
        try
        {
            return op switch
            {
                "=" or "==" => Equal(left, right),
                "!=" or "<>" => !Equal(left, right),
                "<" => Compare(left, right) < 0,
                "<=" => Compare(left, right) <= 0,
                ">" => Compare(left, right) > 0,
                ">=" => Compare(left, right) >= 0,
                _ => throw new ArgumentException(op)
            };
        }
        catch (ArgumentException) { return false; }
    }
    public static object? Coerce(object? value, ColumnType type, string column)
    {
        if (value is null || type == ColumnType.Any)
            return value;
        if (type == ColumnType.Float && Numeric(value) && value is not bool)
            return value is BigInteger bi ? (double)bi : Convert.ToDouble(value, CultureInfo.InvariantCulture);
        bool valid = type switch
        {
            ColumnType.Str => value is string,
            ColumnType.Int => Numeric(value) && value is not bool and not double and not float,
            ColumnType.Bool => value is bool,
            ColumnType.List => value is IList and not byte[],
            ColumnType.Dict => value is IDictionary,
            ColumnType.Bytes => value is byte[],
            ColumnType.Tuple => value is LiteralTuple,
            ColumnType.Set => value is HashSet<object?>,
            ColumnType.Complex => value is Complex,
            _ => false
        };
        return valid ? value : throw new QueryError($"Column {column} holds {TypeName(type)}, got {value.GetType().Name}");
    }
}

public sealed record LiteralTuple(object?[] Items)
{
    public bool Equals(LiteralTuple? other) => other is not null && Values.Equal(Items, other.Items);
    public override int GetHashCode() => 0;
}

/// <summary>A non-executing parser/writer for the Python literal syntax used by .siql files.</summary>
public static class PythonLiteral
{
    public static string Write(object? value) => value switch
    {
        null => "None",
        bool b => b ? "True" : "False",
        string s => Quote(s),
        byte[] bytes => ByteQuote(bytes),
        Complex c when double.IsFinite(c.Real) && double.IsFinite(c.Imaginary) => $"({c.Real.ToString("R", CultureInfo.InvariantCulture)}{(c.Imaginary < 0 ? "" : "+")}{c.Imaginary.ToString("R", CultureInfo.InvariantCulture)}j)",
        double d when double.IsFinite(d) => Float(d),
        float f when float.IsFinite(f) => Float(f),
        _ when Values.Numeric(value) && value is not double and not float => Convert.ToString(value, CultureInfo.InvariantCulture)!,
        LiteralTuple tuple => "(" + string.Join(", ", tuple.Items.Select(Write)) + (tuple.Items.Length == 1 ? "," : "") + ")",
        IDictionary dict => "{" + string.Join(", ", Values.DictionaryEntries(dict).Select(e => Write(e.Key) + ": " + Write(e.Value))) + "}",
        HashSet<object?> set => set.Count == 0 ? "set()" : "{" + string.Join(", ", set.Select(Write)) + "}",
        IList list => "[" + string.Join(", ", list.Cast<object?>().Select(Write)) + "]",
        _ => throw new ArgumentException($"Value {value?.GetType().Name} cannot be stored as a Python literal")
    };
    static string Float(double value)
    {
        string s = value.ToString("R", CultureInfo.InvariantCulture);
        return s.IndexOfAny(['.', 'e', 'E']) < 0 ? s + ".0" : s;
    }
    static string ByteQuote(byte[] bytes)
    {
        char quote = bytes.Contains((byte)'\'') && !bytes.Contains((byte)'"') ? '"' : '\'';
        var b = new StringBuilder("b").Append(quote);
        foreach (byte value in bytes)
            b.Append(value == quote ? "\\" + quote : value switch
            {
                92 => "\\\\",
                9 => "\\t",
                10 => "\\n",
                13 => "\\r",
                >= 32 and < 127 => ((char)value).ToString(),
                _ => $"\\x{value:x2}"
            });
        return b.Append(quote).ToString();
    }
    static string Quote(string text)
    {
        char quote = text.Contains('\'') && !text.Contains('"') ? '"' : '\'';
        var b = new StringBuilder().Append(quote);
        foreach (int cp in Values.CodePoints(text))
        {
            if (cp == quote)
                b.Append('\\').Append(quote);
            else if (cp == '\\')
                b.Append("\\\\");
            else if (cp == '\n')
                b.Append("\\n");
            else if (cp == '\r')
                b.Append("\\r");
            else if (cp == '\t')
                b.Append("\\t");
            else
            {
                var category = cp is >= 0xd800 and <= 0xdfff ? UnicodeCategory.Surrogate : Rune.GetUnicodeCategory(new Rune(cp));
                bool escaped = cp != 32 && category is UnicodeCategory.Control or UnicodeCategory.Format or UnicodeCategory.Surrogate or UnicodeCategory.PrivateUse or UnicodeCategory.OtherNotAssigned or UnicodeCategory.SpaceSeparator or UnicodeCategory.LineSeparator or UnicodeCategory.ParagraphSeparator;
                b.Append(escaped ? cp <= 255 ? $"\\x{cp:x2}" : cp <= 65535 ? $"\\u{cp:x4}" : $"\\U{cp:x8}" : cp <= 65535 ? ((char)cp).ToString() : char.ConvertFromUtf32(cp));
            }
        }
        return b.Append(quote).ToString();
    }
    public static object? Read(string text)
    {
        var p = new Reader(text);
        var value = p.Value();
        p.Space();
        if (p.Position != text.Length)
            throw new FormatException("Trailing literal input");
        return value;
    }
    sealed class Reader(string text)
    {
        public int Position;
        public void Space()
        {
            while (Position < text.Length && char.IsWhiteSpace(text[Position]))
                Position++;
        }
        bool Take(char c)
        {
            Space();
            if (Position < text.Length && text[Position] == c)
            {
                Position++;
                return true;
            }
            return false;
        }
        void Require(char c)
        {
            if (!Take(c))
                throw new FormatException($"Expected {c} at {Position}");
        }
        public object? Value()
        {
            Space();
            if (Position >= text.Length)
                throw new FormatException("Missing literal");
            bool bytes = text[Position] is 'b' or 'B' && Position + 1 < text.Length && text[Position + 1] is '\'' or '"';
            if (bytes)
                Position++;
            if (text[Position] is '\'' or '"')
            {
                string s = String(bytes);
                return bytes ? s.Select(c => checked((byte)c)).ToArray() : s;
            }
            if (Take('['))
                return List(']');
            if (Take('('))
            {
                if (Take(')'))
                    return new LiteralTuple([]);
                var first = Value();
                if (Take(')'))
                    return first;
                Require(',');
                var items = new List<object?> { first };
                while (!Take(')'))
                {
                    items.Add(Value());
                    if (Take(')'))
                        return new LiteralTuple(items.ToArray());
                    Require(',');
                }
                return new LiteralTuple(items.ToArray());
            }
            if (Take('{'))
            {
                if (Take('}'))
                    return new LiteralDictionary();
                var first = Value();
                if (Take(':'))
                {
                    var dict = new LiteralDictionary { [first!] = Value() };
                    while (!Take('}'))
                    {
                        Require(',');
                        if (Take('}'))
                            break;
                        var key = Value();
                        Require(':');
                        dict[key!] = Value();
                    }
                    return dict;
                }
                var set = new HashSet<object?>(PythonEqualityComparer.Instance) { first };
                while (!Take('}'))
                {
                    Require(',');
                    if (Take('}'))
                        break;
                    set.Add(Value());
                }
                return set;
            }
            int start = Position;
            while (Position < text.Length && !char.IsWhiteSpace(text[Position]) && !",:]})(".Contains(text[Position]))
                Position++;
            string token = text[start..Position];
            if (token == "set" && Take('('))
            {
                Require(')');
                return new HashSet<object?>(PythonEqualityComparer.Instance);
            }
            if (token == "None")
                return null;
            if (token == "True")
                return true;
            if (token == "False")
                return false;
            if (token.EndsWith('j'))
            {
                var m = Regex.Match(token, @"\A(?:(?<real>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?<imag>[+-](?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|(?<imag>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?))j\z");
                if (!m.Success)
                    throw new FormatException("Invalid complex literal");
                return new Complex(m.Groups["real"].Success ? double.Parse(m.Groups["real"].Value, CultureInfo.InvariantCulture) : 0, double.Parse(m.Groups["imag"].Value, CultureInfo.InvariantCulture));
            }
            string digits = token.Replace("_", "");
            bool negative = digits.StartsWith('-');
            string unsigned = digits.TrimStart('+', '-');
            BigInteger number;
            if (unsigned.Length > 2 && unsigned[0] == '0' && "xXoObB".Contains(unsigned[1]))
            {
                int radix = char.ToLowerInvariant(unsigned[1]) switch
                {
                    'x' => 16,
                    'o' => 8,
                    _ => 2
                };
                number = 0;
                foreach (char c in unsigned[2..])
                {
                    int digit = c is >= '0' and <= '9' ? c - '0' : char.ToLowerInvariant(c) - 'a' + 10;
                    if (digit < 0 || digit >= radix)
                        throw new FormatException("Invalid radix literal");
                    number = number * radix + digit;
                }
                if (negative)
                    number = -number;
            }
            else if (token.Contains('.') || token.Contains('e') || token.Contains('E'))
                return double.Parse(token, CultureInfo.InvariantCulture);
            else
                number = BigInteger.Parse(digits, CultureInfo.InvariantCulture);
            return number >= long.MinValue && number <= long.MaxValue ? (object)(long)number : number;
        }
        List<object?> List(char end)
        {
            var result = new List<object?>();
            if (Take(end))
                return result;
            do
            {
                result.Add(Value());
                if (Take(end))
                    return result;
                Require(',');
            } while (!Take(end));
            return result;
        }
        string String(bool bytes)
        {
            char quote = text[Position++];
            var b = new StringBuilder();
            while (Position < text.Length)
            {
                char c = text[Position++];
                if (c == quote)
                    return b.ToString();
                if (c != '\\')
                {
                    b.Append(c);
                    continue;
                }
                if (Position == text.Length)
                    break;
                c = text[Position++];
                if (c == 'x' || !bytes && c is 'u' or 'U')
                {
                    int width = c == 'x' ? 2 : c == 'u' ? 4 : 8;
                    int cp = int.Parse(text.AsSpan(Position, width), NumberStyles.HexNumber);
                    Position += width;
                    b.Append(cp <= 65535 ? ((char)cp).ToString() : char.ConvertFromUtf32(cp));
                }
                else if (c is >= '0' and <= '7')
                {
                    int n = c - '0';
                    for (int i = 0; i < 2 && Position < text.Length && text[Position] is >= '0' and <= '7'; i++)
                        n = n * 8 + text[Position++] - '0';
                    b.Append((char)n);
                }
                else if (c == '\n')
                    continue;
                else if ("nrtabfv\\'\"".Contains(c))
                    b.Append(c switch
                    {
                        'n' => '\n',
                        'r' => '\r',
                        't' => '\t',
                        'a' => '\a',
                        'b' => '\b',
                        'f' => '\f',
                        'v' => '\v',
                        _ => c
                    });
                else
                    b.Append('\\').Append(c);
            }
            throw new FormatException("Unterminated literal string");
        }
    }
}

public sealed class PythonEqualityComparer : IEqualityComparer<object?>
{
    public static PythonEqualityComparer Instance { get; } = new();
    public new bool Equals(object? a, object? b) => Values.Equal(a, b);
    public int GetHashCode(object? value) => value is Complex c && c.Imaginary == 0 ? GetHashCode(c.Real) : value is null ? 0 : Values.Numeric(value) && Values.Cacheable(value) ? Values.CacheKey(value).GetHashCode() : value is string s ? s.GetHashCode() : 0;
}

/// <summary>Python-compatible dictionary equality, including NULL and numeric-equivalent keys.</summary>
public sealed class LiteralDictionary : IDictionary
{
    readonly List<DictionaryEntry> items = [];
    int Find(object? key) => items.FindIndex(e => SiQL.Values.Equal(e.Key, key));
    public object? this[object key] { get { int i = Find(key); return i < 0 ? null : items[i].Value; } set { int i = Find(key); var e = new DictionaryEntry(key, value); if (i < 0) items.Add(e); else items[i] = e; } }
    public ICollection Keys => items.Select(e => e.Key).ToArray(); public ICollection Values => items.Select(e => e.Value).ToArray(); public int Count => items.Count;
    public bool IsReadOnly => false; public bool IsFixedSize => false; public bool IsSynchronized => false; public object SyncRoot => this;
    public void Add(object key, object? value)
    {
        if (Contains(key))
            throw new ArgumentException("Duplicate key");
        this[key] = value;
    }
    public void Clear() => items.Clear(); public bool Contains(object key) => Find(key) >= 0; public void Remove(object key)
    {
        int i = Find(key);
        if (i >= 0)
            items.RemoveAt(i);
    }
    public void CopyTo(Array array, int index)
    {
        foreach (var item in items)
            array.SetValue(item, index++);
    }
    public IDictionaryEnumerator GetEnumerator() => new Enumerator(items); IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    sealed class Enumerator(List<DictionaryEntry> items) : IDictionaryEnumerator
    {
        int index = -1; public DictionaryEntry Entry => items[index]; public object Key => Entry.Key; public object? Value => Entry.Value; public object Current => Entry; public bool MoveNext() => ++index < items.Count; public void Reset() => index = -1;
    }
}
