"""Tests for the HTTP server, Linux runtime, Admin UI, Unix sockets, config, systemd unit, and CLI."""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from ..server import ServerConfig, SiQLServer, install_systemd_unit, sd_notify, systemd_unit
from ..server.cli import main as cli_main
from ..server.config import DEFAULT_PORT, TEMPLATE
from ..server.runtime import http_request, process_metrics, watchdog_interval


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ServerTests(unittest.TestCase):
    token = None
    cors_origin = None
    admin_enabled = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        config = ServerConfig(
            host="127.0.0.1",
            port=0,
            data_dir=self._tmp.name,
            token=self.token,
            max_body=10_000,
            admin_enabled=self.admin_enabled,
            cors_origin=self.cors_origin,
        )
        self.server = SiQLServer(config)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self._tmp.cleanup()

    def request(self, path, body=None, *, token=None, raw=None, method=None, parse_json=True):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                raw_body = resp.read()
                parsed = json.loads(raw_body) if parse_json else raw_body.decode("utf-8")
                return resp.status, parsed
        except urllib.error.HTTPError as e:
            with e:
                raw_body = e.read()
                if parse_json and raw_body:
                    try:
                        parsed = json.loads(raw_body)
                    except ValueError:
                        parsed = raw_body.decode("utf-8")
                else:
                    parsed = raw_body.decode("utf-8")
                return e.code, parsed

    def query(self, q, **kw):
        return self.request("/query", {"query": q}, **kw)


class OpenServerTests(ServerTests):
    def test_health(self):
        status, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("uptime_seconds", body)

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

    def test_admin_ui_and_redirect(self):
        self.query("CREATE TABLE secrets (secret_col str); INSERT INTO secrets VALUES ('sensitive-row-value')")
        status, _ = self.request("/", parse_json=False)
        self.assertEqual(status, 302)
        status, html = self.request("/admin", parse_json=False)
        self.assertEqual(status, 200)
        self.assertIn("SiQL Admin", html)
        self.assertIn('id="query-input"', html)
        self.assertNotIn("sensitive-row-value", html)

        self.server.reload_config(ServerConfig(admin_enabled=False))
        self.assertEqual(self.request("/")[0], 404)
        self.assertEqual(self.request("/admin")[0], 404)

    def test_api_endpoints_and_metrics(self):
        self.assertEqual(
            self.request("/api/query", {"query": "CREATE TABLE people (name str, age int); INSERT INTO people VALUES ('Ann', 31), ('Bob', 25)"})[0],
            200,
        )
        status, st = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(st["database"]["tables"], 1)
        self.assertEqual(st["database"]["total_rows"], 2)
        self.assertIn("pid", st["process"])

        status, tbls = self.request("/api/tables")
        self.assertEqual(status, 200)
        self.assertEqual(tbls["tables"][0]["name"], "people")
        self.assertEqual(tbls["tables"][0]["rows"], 2)

        status, detail = self.request("/api/tables/people?limit=1&offset=1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["rows"], [["Bob", 25]])
        self.assertEqual(detail["total_rows"], 2)
        self.assertEqual(detail["schema"], [{"name": "name", "type": "str"}, {"name": "age", "type": "int"}])
        self.assertIn("entries", detail["cache"])

        self.assertEqual(self.request("/api/tables/people?limit=-1")[0], 400)
        self.assertEqual(self.request("/api/tables/missing")[0], 404)
        self.assertEqual(self.request("/api/tables/bad-name")[0], 400)

        status, compacted = self.request("/api/tables/people/compact", method="POST", raw=b"")
        self.assertEqual(status, 200)
        self.assertEqual(compacted["table"], "people")

        status, hist = self.request("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(len(hist["history"]), 1)
        self.assertTrue(hist["history"][0]["ok"])

        status, metrics = self.request("/metrics", parse_json=False)
        self.assertEqual(status, 200)
        self.assertIn("siql_tables_total 1", metrics)
        self.assertIn('siql_table_rows{table="people"} 2', metrics)

        self.assertEqual(self.request("/api/history", method="DELETE")[0], 200)
        self.assertEqual(self.request("/api/history")[1]["history"], [])

        status, dropped = self.request("/api/tables/people", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(dropped["deleted"], "people")
        self.assertEqual(self.request("/api/tables/people")[0], 404)

    def test_cors_and_reload(self):
        self.assertEqual(self.request("/query", method="OPTIONS")[0], 405)
        self.server.reload_config(ServerConfig(cors_origin="*", history_limit=2, max_body=5000))
        code, _ = http_request(self.url, "/query", method="OPTIONS")
        self.assertEqual(code, 204)


class TokenServerTests(ServerTests):
    token = "s3cret"

    def test_token_required(self):
        self.assertEqual(self.request("/health")[0], 200)
        self.assertEqual(self.request("/admin", parse_json=False)[0], 200)
        for path, method in [
            ("/tables", "GET"),
            ("/api/status", "GET"),
            ("/api/tables", "GET"),
            ("/api/tables/t", "GET"),
            ("/api/history", "GET"),
            ("/api/history", "DELETE"),
            ("/metrics", "GET"),
            ("/api/tables/t/compact", "POST"),
            ("/api/tables/t", "DELETE"),
        ]:
            with self.subTest(path=path, method=method):
                self.assertEqual(self.request(path, method=method)[0], 401)
                self.assertEqual(self.request(path, method=method, token="wrong")[0], 401)
        self.assertEqual(self.query("SHOW TABLES")[0], 401)
        self.assertEqual(self.request("/tables", token=self.token), (200, {"tables": []}))
        self.assertEqual(self.query("SHOW TABLES", token=self.token)[0], 200)
        self.assertEqual(self.request("/api/status", token=self.token)[0], 200)
        self.assertEqual(self.request("/api/tables/missing", token=self.token)[0], 404)


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
        self.assertEqual((config.admin_enabled, config.history_limit, config.unix_socket, config.cors_origin), (True, 100, None, None))
        self.assertEqual(config.data_dir, os.path.join(self._tmp.name, "data"))

    def test_settings(self):
        absolute = os.path.join(self._tmp.name, "elsewhere")
        self.write(
            f'[server]\nport = 9000\ntoken = "abc"\ndata_dir = {json.dumps(absolute)}\n'
            'unix_socket = "siql.sock"\nadmin_enabled = false\nhistory_limit = 25\ncors_origin = "*"\n'
        )
        config = ServerConfig.load(self.path)
        self.assertEqual((config.port, config.token, config.data_dir), (9000, "abc", absolute))
        self.assertEqual(config.unix_socket, os.path.join(self._tmp.name, "siql.sock"))
        self.assertFalse(config.admin_enabled)
        self.assertEqual(config.history_limit, 25)
        self.assertEqual(config.cors_origin, "*")

    def test_invalid_settings(self):
        for text in [
            '[server]\nprot = 1\n',
            '[server]\nport = "80"\n',
            '[server]\nport = true\n',
            '[server]\nport = 70000\n',
            '[server]\nhost = 1\n',
            '[server]\nadmin_enabled = "yes"\n',
            '[server]\nhistory_limit = -1\n',
            '[server]\nhistory_limit = true\n',
        ]:
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(ValueError):
                    ServerConfig.load(self.path)


class UnixSocketAndRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX required")
    def test_unix_socket_server_and_client(self):
        with tempfile.TemporaryDirectory(prefix="siql-") as tmp:
            sock_path = os.path.join(tmp, "s.sock")
            data_dir = os.path.join(tmp, "data")
            server = SiQLServer(ServerConfig(host="127.0.0.1", port=0, data_dir=data_dir, unix_socket=sock_path))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertTrue(os.path.exists(sock_path))
                code, health = http_request("", "/health", unix_socket=sock_path)
                self.assertEqual(code, 200)
                self.assertEqual(health["status"], "ok")

                code, res = http_request("", "/query", method="POST", body={"query": "CREATE TABLE u (x int); INSERT INTO u VALUES (7)"}, unix_socket=sock_path)
                self.assertEqual(code, 200)
                self.assertEqual(len(res["results"]), 2)

                code, status = http_request("", "/api/status", unix_socket=sock_path)
                self.assertEqual(code, 200)
                self.assertEqual(status["unix_socket"], sock_path)
                self.assertEqual(status["database"]["tables"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertFalse(os.path.exists(sock_path))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX required")
    def test_sd_notify_and_watchdog_and_metrics(self):
        with tempfile.TemporaryDirectory(prefix="siql-notify-") as tmp:
            sock_path = os.path.join(tmp, "notify.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(sock_path)
                receiver.settimeout(2.0)
                old_sock = os.environ.get("NOTIFY_SOCKET")
                os.environ["NOTIFY_SOCKET"] = sock_path
                try:
                    self.assertTrue(sd_notify("READY=1\nSTATUS=ok"))
                    self.assertEqual(receiver.recv(1024), b"READY=1\nSTATUS=ok")
                finally:
                    if old_sock is None:
                        os.environ.pop("NOTIFY_SOCKET", None)
                    else:
                        os.environ["NOTIFY_SOCKET"] = old_sock

        old_usec = os.environ.get("WATCHDOG_USEC")
        old_pid = os.environ.get("WATCHDOG_PID")
        try:
            os.environ["WATCHDOG_USEC"] = "4000000"
            os.environ["WATCHDOG_PID"] = str(os.getpid())
            self.assertEqual(watchdog_interval(), 2.0)
            os.environ["WATCHDOG_PID"] = str(os.getpid() + 99999)
            self.assertIsNone(watchdog_interval())
            os.environ["WATCHDOG_USEC"] = "bad"
            os.environ.pop("WATCHDOG_PID", None)
            self.assertIsNone(watchdog_interval())
        finally:
            if old_usec is None:
                os.environ.pop("WATCHDOG_USEC", None)
            else:
                os.environ["WATCHDOG_USEC"] = old_usec
            if old_pid is None:
                os.environ.pop("WATCHDOG_PID", None)
            else:
                os.environ["WATCHDOG_PID"] = old_pid

        metrics = process_metrics()
        self.assertEqual(metrics["pid"], os.getpid())
        if sys.platform.startswith("linux"):
            self.assertIsInstance(metrics["rss_bytes"], int)
            self.assertIsInstance(metrics["threads"], int)
            self.assertIsInstance(metrics["open_fds"], int)


class ServiceTests(unittest.TestCase):
    def test_systemd_unit(self):
        unit = systemd_unit("/srv/siql/siql.toml", python="/usr/bin/python3", user="siql")
        self.assertIn(f"ExecStart=/usr/bin/python3 -m {__package__.rpartition('.')[0]}.server serve --config ", unit)
        self.assertIn("siql.toml", unit.split("ExecStart=")[1].splitlines()[0])
        self.assertIn("Type=notify", unit)
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", unit)
        self.assertIn("WatchdogSec=30s", unit)
        self.assertIn("User=siql", unit)
        self.assertIn("[Install]\nWantedBy=multi-user.target", unit)
        self.assertNotIn("User=", systemd_unit("siql.toml", python="/usr/bin/python3"))

        simple = systemd_unit("siql.toml", python="/usr/bin/python3", notify=False)
        self.assertIn("Type=simple", simple)
        self.assertNotIn("WatchdogSec=", simple)

    def test_systemd_quoting(self):
        unit = systemd_unit("/srv/siql/siql.toml", python="/opt/my python/bin/python 100%")
        self.assertIn('ExecStart="/opt/my python/bin/python 100%%" -m', unit)

    def test_install_systemd_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "siql.toml")
            unit_dir = os.path.join(tmp, "systemd")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(TEMPLATE)
            written = install_systemd_unit(cfg, name="siql-test", unit_dir=unit_dir, enable=False)
            self.assertEqual(written, os.path.join(unit_dir, "siql-test.service"))
            self.assertTrue(os.path.isfile(written))
            with self.assertRaises(ValueError):
                install_systemd_unit(cfg, name="../bad", unit_dir=unit_dir)


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

    def test_init_and_systemd_and_install(self):
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
        self.assertIn("Type=notify", out)

        code, out, _ = self.run_cli("systemd", "-c", self.config, "--simple")
        self.assertEqual(code, 0)
        self.assertIn("Type=simple", out)

        unit_file = os.path.join(self._tmp.name, "siql.service")
        code, out, _ = self.run_cli("systemd", "-c", self.config, "-o", unit_file)
        self.assertEqual(code, 0)
        self.assertIn("systemctl enable --now siql", out)
        self.assertTrue(os.path.isfile(unit_file))

        unit_dir = os.path.join(self._tmp.name, "units")
        code, out, _ = self.run_cli("install", "-c", self.config, "--unit-dir", unit_dir, "--name", "mysiql")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(unit_dir, "mysiql.service")))

    def test_systemd_needs_config(self):
        code, _, err = self.run_cli("systemd", "-c", self.config)
        self.assertEqual(code, 1)
        self.assertIn("siql-server init", err)

    def test_status_and_query_cli(self):
        server = SiQLServer(ServerConfig(host="127.0.0.1", port=0, data_dir=self._tmp.name))
        url = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code, out, _ = self.run_cli("status", "--url", url)
            self.assertEqual(code, 0)
            self.assertIn("SiQL ", out)

            code, out, _ = self.run_cli("status", "--url", url, "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["status"], "ok")

            code, out, _ = self.run_cli("query", "--url", url, "CREATE TABLE items (name str); INSERT INTO items VALUES ('ok'); SELECT * FROM items")
            self.assertEqual(code, 0)
            self.assertIn("Created table items", out)
            self.assertIn("ok", out)

            code, out, _ = self.run_cli("query", "--url", url, "--json", "SELECT * FROM items")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["results"][0]["rows"], [["ok"]])

            code, _, err = self.run_cli("query", "--url", url, "SELECT * FROM nope")
            self.assertEqual(code, 1)
            self.assertIn("Error:", err)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
