"""An MCP server (Model Context Protocol, JSON-RPC over stdio) that lets an AI agent work with a SiQL database folder.

    siql-mcp --data-dir data                      # read-only: SELECT, SHOW TABLES, DESCRIBE, search
    siql-mcp --data-dir data --write              # every statement except the ones that touch other files
    siql-mcp --data-dir data --embed mymodels:embed_text --path /opt/app   # + text search on vector columns

Register it with an MCP client, for example Claude Code:

    claude mcp add siql -- siql-mcp --data-dir /srv/app/data

Tools: sql_help, list_tables, describe, query, search. Each call opens the folder again, so the agent sees what other
processes wrote since the last call. Read-only is the default because a folder must have one writing process only: give
--write only when no other process writes to the folder.

--embed names a function that turns text into a vector (`module:function`, called as f(text) and returning a sequence
of numbers, the same model that made the column's vectors). Without it, search finds rows like a given row or a given
vector, but not like a text.

The server uses only the standard library.
"""
from __future__ import annotations

import argparse
import json
import sys
from array import array
from importlib import import_module
from typing import Any, Callable

from .controllers import Interpreter, QueryError
from .controllers.parser import Describe, Select, ShowTables, parse
from .helpers.types import type_name
from .helpers.vector import Vector
from .managers import Database

PROTOCOL = "2025-06-18"
READ_ONLY = (Select, ShowTables, Describe)
TEXT_LIMIT = 2000  # longer string cells are cut, so one big cell does not fill the agent's context

SQL_HELP = """SiQL query language (one statement per query call):
  SHOW TABLES;  DESCRIBE t;
  SELECT a, b FROM t WHERE x = 1 AND (y > 2 OR z LIKE 'ab%') AND w IN (1, 2) AND v IS NOT NULL
         ORDER BY a DESC LIMIT 10 OFFSET 20;
  SELECT COUNT(*) FROM t WHERE ...;     SELECT DISTINCT a FROM t;
  SELECT id FROM t WHERE kind = 'x' ORDER BY COSINE(vec, [0.1, -0.4, ...]) DESC LIMIT 5;   (also DOT)
  With write access: INSERT INTO t VALUES (...), (...); UPDATE t SET a = 1 WHERE ...; DELETE FROM t WHERE ...;
  CREATE TABLE t (a str, b int, c float, d bool, v vector); ALTER TABLE t ADD COLUMN e str; DROP TABLE t;
Strings use single quotes ('it''s'). Column types: str, int, float, bool, vector. Vector cells are shown as
<vector N>; use the search tool to find rows by meaning instead of writing vectors by hand."""


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required or []}}


class Server:
    """The tools, without the stdio transport: `handle(message)` takes one JSON-RPC message and returns the reply
    (None for a notification)."""

    def __init__(self, data_dir: str, *, write: bool = False, embed: Callable[[str], Any] | str | None = None,
                 max_rows: int = 200):
        self.data_dir, self.write, self.max_rows = data_dir, write, max_rows
        self._embed, self._embed_name = (None, embed) if isinstance(embed, str) else (embed, None)

    # tools -----------------------------------------------------------------
    def tools(self) -> list[dict]:
        mode = "Any statement except USE, IMPORT and EXPORT." if self.write else (
            "Read-only: SELECT, SHOW TABLES and DESCRIBE only.")
        out = [
            _tool("sql_help", "The SiQL query language in short: read this before the first query.", {}),
            _tool("list_tables", "All tables with their row count and columns (name: type).", {}),
            _tool("describe", "One table's columns and types, its row count and its first rows.",
                  {"table": {"type": "string"}, "sample": {"type": "integer", "description": "rows to show (default 3)"}},
                  ["table"]),
            _tool("query", f"Run one SiQL statement and get its columns and rows. {mode} "
                           f"At most max_rows rows come back (default {self.max_rows}).",
                  {"sql": {"type": "string"}, "max_rows": {"type": "integer"}}, ["sql"]),
            _tool("search", "Rows whose vector column is most like a text (needs the server's embedder), like another "
                            "row's vector, or like a given vector; best first, with the score (cosine 1 = same).",
                  {"table": {"type": "string"},
                   "column": {"type": "string", "description": "the vector column (default: the table's only one)"},
                   "text": {"type": "string", "description": "find rows that mean this"},
                   "like": {"type": "object", "description": "{column: value} of a row: find rows like that row"},
                   "vector": {"type": "array", "items": {"type": "number"}},
                   "where": {"type": "object", "description": "{column: value} equality filters"},
                   "columns": {"type": "array", "items": {"type": "string"}, "description": "columns to return"},
                   "k": {"type": "integer", "description": "rows to return (default 10)"},
                   "metric": {"type": "string", "enum": ["cosine", "dot"]}},
                  ["table"]),
        ]
        return out

    def call(self, name: str, args: dict) -> Any:
        fn = {"sql_help": lambda a: SQL_HELP, "list_tables": self.list_tables, "describe": self.describe,
              "query": self.query, "search": self.search}.get(name)
        if fn is None:
            raise ValueError(f"No such tool: {name}")
        return fn(args)

    def db(self) -> Database:
        return Database(self.data_dir)  # opened again for each call: other processes may have written

    def list_tables(self, _args: dict) -> list[dict]:
        db = self.db()
        out = []
        for name in db.tables():
            t = db.table(name)
            out.append({"table": name, "rows": len(t.rows), "columns": _types(t)})
        return out

    def describe(self, args: dict) -> dict:
        t = self.db().table(args["table"])
        n = max(0, int(args.get("sample", 3)))
        names = t.struct.column_names
        return {"table": args["table"], "rows": len(t.rows), "columns": _types(t),
                "sample": [{c: _show(r.col(c).value) for c in names} for r in t.rows[:n]]}

    def query(self, args: dict) -> dict:
        sql = args["sql"]
        statements = parse(sql)
        if len(statements) != 1:
            raise QueryError(f"query runs one statement, got {len(statements)}")
        if not self.write and not isinstance(statements[0], READ_ONLY):
            raise QueryError(f"Read-only server: {type(statements[0]).__name__} is not allowed "
                             "(start the server with --write to change data)")
        res = Interpreter(self.db(), file_access=False).run(statements[0])
        limit = int(args.get("max_rows", self.max_rows))
        rows = [[_show(v) for v in r] for r in res.rows[:limit]]
        out = {"columns": res.columns, "rows": rows, "rowcount": res.rowcount}
        if res.message: out["message"] = res.message
        if len(res.rows) > limit: out["truncated"] = f"{len(res.rows) - limit} more rows; use LIMIT/OFFSET or WHERE"
        return out

    def search(self, args: dict) -> dict:
        t = self.db().table(args["table"])
        column = args.get("column") or _vector_column(t)
        given = [k for k in ("text", "like", "vector") if args.get(k) is not None]
        if len(given) != 1:
            raise ValueError("Give exactly one of text, like or vector")
        if given[0] == "text":
            q = list(self.embedder()(args["text"]))
        elif given[0] == "like":
            (col, value), = args["like"].items()
            hits = t.find_where(**{col: value})
            if not hits:
                raise ValueError(f"No row with {col} = {value!r}")
            q = t.rows[hits[0]].col(column).value
            if q is None:
                raise ValueError(f"That row has no vector in {column}")
        else:
            q = args["vector"]
        k = int(args.get("k", 10))
        names = args.get("columns") or [c for c in t.struct.column_names if c != column]
        hits = t.nearest(column, Vector.unpack(q) if isinstance(q, (bytes, str)) else q, k=k,
                         where=args.get("where"), metric=args.get("metric", "cosine"))
        return {"column": column, "results": [
            {"score": round(float(score), 4), **{c: _show(t.rows[i].col(c).value) for c in names}} for i, score in hits]}

    def embedder(self) -> Callable[[str], Any]:
        if self._embed is None:
            if not self._embed_name:
                raise ValueError("This server has no embedder for text search (start it with --embed module:function); "
                                 "search with like or vector instead")
            mod, _, fn = self._embed_name.partition(":")
            self._embed = getattr(import_module(mod), fn)
        return self._embed

    # JSON-RPC --------------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # a notification (initialized, cancelled): no reply
            return None
        try:
            if method == "initialize":
                result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "siql", "version": _version()},
                          "instructions": "SiQL database tools. Call sql_help before the first query and list_tables "
                                          "to see the data. Use search to find rows by meaning." +
                                          ("" if self.write else " This server is read-only.")}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                p = msg.get("params", {})
                try:
                    value = self.call(p.get("name"), p.get("arguments") or {})
                    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                except (QueryError, ValueError, KeyError, TypeError, OSError, ImportError, AttributeError) as e:
                    result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
            elif method == "ping":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        except Exception as e:  # never let one bad message stop the server
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=None, stdout=None):
        """Read one JSON message per line from stdin, write each reply as one line to stdout."""
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def _types(t) -> dict:
    return {n: type_name(ty).lower() for n, ty in zip(t.struct.column_names, t.struct.columns)}


def _vector_column(t) -> str:
    vec = [c for c, tp in _types(t).items() if tp == "vector"]
    if len(vec) != 1:
        raise ValueError(f"Name the vector column: the table has {len(vec)} ({', '.join(vec) or 'none'})")
    return vec[0]


def _show(v):
    """A cell as JSON: vectors as <vector N>, long text cut."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<vector {len(v) // 4}>"
    if isinstance(v, array):
        return f"<vector {len(v)}>"
    if isinstance(v, str) and len(v) > TEXT_LIMIT:
        return v[:TEXT_LIMIT] + f"... ({len(v)} characters)"
    return v


def _version() -> str:
    from . import __version__
    return __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="siql-mcp", description="MCP server (stdio) for an agent to use a SiQL database.")
    ap.add_argument("--data-dir", default=".", help="the folder of .siql tables (default: .)")
    ap.add_argument("--write", action="store_true", help="allow statements that change data (one writer per folder!)")
    ap.add_argument("--embed", help="module:function that turns text into a vector, for text search")
    ap.add_argument("--path", action="append", default=[], help="a folder to add to sys.path for --embed (repeatable)")
    ap.add_argument("--max-rows", type=int, default=200, help="most rows one query returns (default 200)")
    a = ap.parse_args(argv)
    sys.path[:0] = a.path
    Server(a.data_dir, write=a.write, embed=a.embed, max_rows=a.max_rows).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
