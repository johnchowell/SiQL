"""Tests for the MCP server (siql.mcp): the JSON-RPC handshake, the tools, read-only mode, search by text, row and
vector, and the stdio loop."""
import io
import json
import sys
import tempfile
import types
import unittest

from ..controllers import Interpreter
from ..mcp import Server, main


def embed_text(text: str):
    """A toy embedder: one axis per known word."""
    words = ["vault", "money", "kitchen"]
    return [1.0 if w in text else 0.0 for w in words]


class McpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        Interpreter(self.dir).executescript(
            "CREATE TABLE assets (id str, kind str, vec vector);"
            "INSERT INTO assets VALUES ('a', 'background', [1, 0, 0]), ('b', 'object', [0, 1, 0]),"
            " ('c', 'background', [0, 0, 1]), ('d', 'object', [0.9, 0.1, 0]);")

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, server, name, **args):
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
        res = reply["result"]
        text = res["content"][0]["text"]
        if res["isError"]:
            return ("error", text)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def test_handshake_and_tool_list(self):
        s = Server(self.dir)
        init = s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["sql_help", "list_tables", "describe", "query", "search"])
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_list_tables_and_describe(self):
        s = Server(self.dir)
        self.assertEqual(self.call(s, "list_tables"),
                         [{"table": "assets", "rows": 4, "columns": {"id": "str", "kind": "str", "vec": "vector"}}])
        d = self.call(s, "describe", table="assets", sample=1)
        self.assertEqual(d["sample"], [{"id": "a", "kind": "background", "vec": "<vector 3>"}])
        self.assertEqual(self.call(s, "describe", table="missing")[0], "error")

    def test_query_read_only(self):
        s = Server(self.dir)
        r = self.call(s, "query", sql="SELECT id FROM assets WHERE kind = 'object' ORDER BY id")
        self.assertEqual(r["rows"], [["b"], ["d"]])
        err = self.call(s, "query", sql="DELETE FROM assets WHERE id = 'a'")
        self.assertEqual(err[0], "error")
        self.assertIn("Read-only", err[1])
        self.assertEqual(self.call(s, "query", sql="SELECT COUNT(*) FROM assets")["rows"], [[4]])

    def test_query_write_and_row_limit(self):
        s = Server(self.dir, write=True, max_rows=2)
        self.call(s, "query", sql="DELETE FROM assets WHERE id = 'a'")
        r = self.call(s, "query", sql="SELECT id FROM assets ORDER BY id")
        self.assertEqual(r["rows"], [["b"], ["c"]])
        self.assertIn("1 more rows", r["truncated"])
        self.assertEqual(self.call(s, "query", sql="USE '/tmp'")[0], "error")  # no file access, even with --write

    def test_sees_other_writers(self):
        s = Server(self.dir)
        self.call(s, "list_tables")
        Interpreter(self.dir).execute("INSERT INTO assets VALUES ('e', 'object', [0, 1, 1])")
        self.assertEqual(self.call(s, "list_tables")[0]["rows"], 5)

    def test_search_like_row_vector_and_filter(self):
        s = Server(self.dir)
        r = self.call(s, "search", table="assets", like={"id": "a"}, k=2, columns=["id"])
        self.assertEqual([x["id"] for x in r["results"]], ["a", "d"])
        self.assertEqual(r["column"], "vec")
        r = self.call(s, "search", table="assets", vector=[0, 0, 1], k=1, columns=["id"])
        self.assertEqual(r["results"][0]["id"], "c")
        r = self.call(s, "search", table="assets", vector=[1, 0, 0], where={"kind": "object"}, k=1)
        self.assertEqual(r["results"][0]["id"], "d")
        self.assertEqual(r["results"][0]["kind"], "object")

    def test_search_text_needs_embedder(self):
        err = self.call(Server(self.dir), "search", table="assets", text="money")
        self.assertEqual(err[0], "error")
        self.assertIn("--embed", err[1])
        r = self.call(Server(self.dir, embed=embed_text), "search", table="assets", text="money", k=1)
        self.assertEqual(r["results"][0]["id"], "b")

    def test_search_text_with_named_embedder(self):
        mod = types.ModuleType("siql_test_embedder")
        mod.embed = embed_text
        sys.modules["siql_test_embedder"] = mod
        try:
            r = self.call(Server(self.dir, embed="siql_test_embedder:embed"), "search", table="assets", text="kitchen", k=1)
            self.assertEqual(r["results"][0]["id"], "c")
        finally:
            del sys.modules["siql_test_embedder"]

    def test_search_needs_one_query(self):
        s = Server(self.dir)
        self.assertEqual(self.call(s, "search", table="assets")[0], "error")
        self.assertEqual(self.call(s, "search", table="assets", like={"id": "zz"})[0], "error")

    def test_stdio_loop(self):
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 "not json",
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "query", "arguments": {"sql": "SHOW TABLES"}}})]
        out = io.StringIO()
        Server(self.dir).serve(io.StringIO("\n".join(lines) + "\n"), out)
        replies = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertIn("assets", replies[2]["result"]["content"][0]["text"])

    def test_main_parses_arguments(self):
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"), io.StringIO()
        try:
            self.assertEqual(main(["--data-dir", self.dir]), 0)
            self.assertEqual(json.loads(sys.stdout.getvalue())["result"], {})
        finally:
            sys.stdin, sys.stdout = stdin, stdout


if __name__ == "__main__":
    unittest.main()
