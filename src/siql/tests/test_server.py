"""Tests for the optional HTTP server, its config file, the systemd unit and the siql-server command line."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from ..server import ServerConfig, SiQLServer, systemd_unit
from ..server.cli import main as cli_main
from ..server.config import TEMPLATE, DEFAULT_PORT


class ServerTests(unittest.TestCase):
    token = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        config = ServerConfig(host="127.0.0.1", port=0, data_dir=self._tmp.name, token=self.token, max_body=10_000)
        self.server = SiQLServer(config)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self._tmp.cleanup()

    def request(self, path, body=None, *, token=None, raw=None, method=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def query(self, q, **kw):
        return self.request("/query", {"query": q}, **kw)


class OpenServerTests(ServerTests):
    def test_health(self):
        status, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_query_flow(self):
        status, body = self.query("CREATE TABLE t (name str, n int); INSERT INTO t VALUES ('a', 1), ('b', 2)")
        self.assertEqual(status, 200)
        self.assertEqual([r["message"] for r in body["results"]], ["Created table t", "Inserted 2 rows"])
        status, body = self.query("SELECT * FROM t WHERE n > 1")
        self.assertEqual(body["results"], [{"columns": ["name", "n"], "rows": [["b", 2]], "rowcount": 1, "message": "1 row"}])
        self.assertEqual(self.request("/tables"), (200, {"tables": ["t"]}))
        self.assertTrue(os.path.isfile(os.path.join(self._tmp.name, "t.siql")))

    def test_query_error_reports_completed_statements(self):
        status, body = self.query("CREATE TABLE t (x int); SELECT * FROM missing; CREATE TABLE u (x int)")
        self.assertEqual(status, 400)
        self.assertIn("No such table", body["error"])
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(self.request("/tables")[1], {"tables": ["t"]})

    def test_syntax_error_runs_nothing(self):
        status, body = self.query("CREATE TABLE t (x int); SELEC")
        self.assertEqual((status, body["results"]), (400, []))
        self.assertEqual(self.request("/tables")[1], {"tables": []})

    def test_file_statements_are_disabled(self):
        for query in ["USE '..'", "EXPORT t TO 'x.csv'", "IMPORT 'x.csv' INTO t"]:
            with self.subTest(query=query):
                status, body = self.query(query)
                self.assertEqual(status, 400)
                self.assertIn("disabled", body["error"])

    def test_bad_requests(self):
        self.assertEqual(self.request("/query", raw=b"not json")[0], 400)
        self.assertEqual(self.request("/query", [1, 2])[0], 400)
        self.assertEqual(self.request("/query", {"query": 5})[0], 400)
        self.assertEqual(self.request("/query", {"query": "x" * 20_000})[0], 413)
        self.assertEqual(self.request("/nope")[0], 404)
        self.assertEqual(self.request("/nope", {})[0], 404)


class TokenServerTests(ServerTests):
    token = "s3cret"

    def test_token_required(self):
        self.assertEqual(self.request("/health")[0], 200)
        self.assertEqual(self.request("/tables")[0], 401)
        self.assertEqual(self.request("/tables", token="wrong")[0], 401)
        self.assertEqual(self.query("SHOW TABLES")[0], 401)
        self.assertEqual(self.request("/tables", token=self.token), (200, {"tables": []}))
        self.assertEqual(self.query("SHOW TABLES", token=self.token)[0], 200)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "siql.toml")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_template_gives_defaults(self):
        self.write(TEMPLATE)
        config = ServerConfig.load(self.path)
        self.assertEqual((config.host, config.port, config.token), ("127.0.0.1", DEFAULT_PORT, None))
        self.assertEqual(config.data_dir, os.path.join(self._tmp.name, "data"))

    def test_settings(self):
        absolute = os.path.join(self._tmp.name, "elsewhere")
        self.write(f'[server]\nport = 9000\ntoken = "abc"\ndata_dir = {json.dumps(absolute)}\n')
        config = ServerConfig.load(self.path)
        self.assertEqual((config.port, config.token, config.data_dir), (9000, "abc", absolute))

    def test_invalid_settings(self):
        for text in ['[server]\nprot = 1\n', '[server]\nport = "80"\n', '[server]\nport = true\n',
                     '[server]\nport = 70000\n', '[server]\nhost = 1\n']:
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(ValueError):
                    ServerConfig.load(self.path)


class ServiceTests(unittest.TestCase):
    def test_systemd_unit(self):
        unit = systemd_unit("/srv/siql/siql.toml", python="/usr/bin/python3", user="siql")
        self.assertIn(f"ExecStart=/usr/bin/python3 -m {__package__.rpartition('.')[0]}.server serve --config ", unit)
        self.assertIn("siql.toml", unit.split("ExecStart=")[1].splitlines()[0])
        self.assertIn("User=siql", unit)
        self.assertIn("[Install]\nWantedBy=multi-user.target", unit)
        self.assertNotIn("User=", systemd_unit("siql.toml", python="/usr/bin/python3"))

    def test_systemd_quoting(self):
        unit = systemd_unit("/srv/siql/siql.toml", python="/opt/my python/bin/python 100%")
        self.assertIn('ExecStart="/opt/my python/bin/python 100%%" -m', unit)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self._tmp.name, "siql.toml")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_init_and_systemd(self):
        self.assertEqual(self.run_cli("init", "-c", self.config)[0], 0)
        with open(self.config, encoding="utf-8") as f:
            self.assertEqual(f.read(), TEMPLATE)
        code, _, err = self.run_cli("init", "-c", self.config)
        self.assertEqual(code, 1)
        self.assertIn("siql-server:", err)
        self.assertEqual(self.run_cli("init", "-c", self.config, "--force")[0], 0)

        code, out, _ = self.run_cli("systemd", "-c", self.config, "--user", "svc")
        self.assertEqual(code, 0)
        self.assertIn("User=svc", out)
        unit_file = os.path.join(self._tmp.name, "siql.service")
        code, out, _ = self.run_cli("systemd", "-c", self.config, "-o", unit_file)
        self.assertEqual(code, 0)
        self.assertIn("systemctl enable --now siql", out)
        self.assertTrue(os.path.isfile(unit_file))

    def test_systemd_needs_config(self):
        code, _, err = self.run_cli("systemd", "-c", self.config)
        self.assertEqual(code, 1)
        self.assertIn("siql-server init", err)


if __name__ == "__main__":
    unittest.main()
