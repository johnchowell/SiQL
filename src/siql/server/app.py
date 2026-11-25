"""HTTP + JSON API, Unix socket listener, Linux daemon runtime, and Admin Web UI for a SiQL Database.

    GET  /                          -> 302 redirect to /admin (when admin_enabled)
    GET  /admin                     -> Self-contained Admin Web UI with query block
    GET  /health                    -> {"status": "ok", "version": "...", "uptime_seconds": ...} (no token needed)
    GET  /tables                    -> {"tables": ["people", ...]}
    POST /query  {"query": "..."}   -> {"results": [{"columns", "rows", "rowcount", "message"}, ...], "elapsed_ms": ...}
    GET  /api/status                -> runtime, database, process, and counter telemetry
    GET  /api/tables                -> {"tables": [{"name", "rows", "columns", "bytes"}, ...]}
    GET  /api/tables/<name>         -> schema, cache stats, and paginated rows (?limit=50&offset=0)
    POST /api/tables/<name>/compact -> {"table", "bytes_before", "bytes_after"}
    DELETE /api/tables/<name>       -> {"deleted", "message"}
    POST /api/query                 -> alias for POST /query
    GET  /api/history               -> {"history": [...]}
    DELETE /api/history             -> {"cleared": true}
    GET  /metrics                   -> Prometheus text exposition metrics

Errors return {"error": "..."}. A failed query also returns the results of the statements before it,
which were already saved.
"""
import collections
import hmac
import ipaddress
import json
import logging
import os
import signal
import socket
import socketserver
import stat
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__
from ..controllers import Interpreter, QueryError, parse
from ..helpers.types import type_name
from ..managers import Database
from .admin import ADMIN_HTML
from .config import ServerConfig, TOKEN_ENV
from .runtime import process_metrics, sd_notify, watchdog_interval

log = logging.getLogger(__package__)


class _Reject(Exception):
    def __init__(self, status: HTTPStatus, message: str, **extra):
        super().__init__(message)
        self.status, self.payload = status, {"error": message, **extra}


if hasattr(socketserver, "ThreadingUnixStreamServer"):
    class _UnixSiQLServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

        def __init__(self, socket_path: str, handler_class, *, owner: "SiQLServer"):
            self.owner = owner
            super().__init__(socket_path, handler_class)
else:  # pragma: no cover - non-POSIX platforms
    _UnixSiQLServer = None


class SiQLServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: ServerConfig):
        self.config = config
        self.database = Database(config.data_dir)
        self.interpreter = Interpreter(self.database, file_access=False)
        self.lock = threading.Lock()  # tables aren't thread-safe, so requests use them one at a time
        self._meta_lock = threading.Lock()
        self.started_at = time.time()
        self.started_iso = datetime.now(timezone.utc).isoformat()
        self._stats = {"requests": 0, "queries": 0, "errors": 0}
        self._history: collections.deque[dict] = collections.deque(maxlen=config.history_limit)
        self._history_seq = 0
        self._unix_server = None
        self._unix_thread: threading.Thread | None = None
        super().__init__((config.host, config.port), _Handler)
        if config.unix_socket:
            try:
                self._bind_unix_socket(config.unix_socket)
            except Exception:
                super().server_close()
                raise

    def _bind_unix_socket(self, socket_path: str) -> None:
        if _UnixSiQLServer is None or not hasattr(socket, "AF_UNIX"):
            raise OSError("Unix domain sockets are not supported on this platform")
        parent = os.path.dirname(os.path.abspath(socket_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.lexists(socket_path):
            if not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
                raise OSError(f"Refusing to overwrite non-socket path: {socket_path}")
            os.unlink(socket_path)
        self._unix_server = _UnixSiQLServer(socket_path, _Handler, owner=self)
        os.chmod(socket_path, 0o660)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if self._unix_server is not None:
            self._unix_thread = threading.Thread(
                target=self._unix_server.serve_forever,
                kwargs={"poll_interval": poll_interval},
                daemon=True,
            )
            self._unix_thread.start()
        super().serve_forever(poll_interval=poll_interval)

    def shutdown(self) -> None:
        super().shutdown()
        if self._unix_server is not None:
            self._unix_server.shutdown()
            if self._unix_thread is not None and self._unix_thread.is_alive():
                self._unix_thread.join(timeout=5.0)

    def server_close(self) -> None:
        super().server_close()
        if self._unix_server is not None:
            try:
                self._unix_server.server_close()
            finally:
                if self.config.unix_socket:
                    try:
                        os.unlink(self.config.unix_socket)
                    except FileNotFoundError:
                        pass

    def reload_config(self, new_config: ServerConfig) -> None:
        """Apply hot-reloadable configuration fields (token, limits, logging, admin, CORS)."""
        with self.lock, self._meta_lock:
            if (new_config.host != self.config.host or new_config.port != self.config.port
                    or os.path.abspath(new_config.data_dir) != os.path.abspath(self.config.data_dir)
                    or new_config.unix_socket != self.config.unix_socket):
                log.warning("Host, port, data_dir, and unix_socket changes require a server restart to take effect")
            self.config.token = new_config.token
            self.config.max_body = new_config.max_body
            self.config.log_level = new_config.log_level
            self.config.admin_enabled = new_config.admin_enabled
            self.config.cors_origin = new_config.cors_origin
            if new_config.history_limit != self.config.history_limit:
                self.config.history_limit = new_config.history_limit
                self._history = collections.deque(self._history, maxlen=new_config.history_limit)

    def count_request(self) -> None:
        with self._meta_lock:
            self._stats["requests"] += 1

    def record_query(self, query: str, ok: bool, statements: int, elapsed_ms: float, error: str | None = None) -> None:
        with self._meta_lock:
            self._stats["queries"] += 1
            if not ok:
                self._stats["errors"] += 1
            if self.config.history_limit > 0:
                self._history_seq += 1
                self._history.appendleft({
                    "id": self._history_seq,
                    "query": query,
                    "ok": ok,
                    "statements": statements,
                    "elapsed_ms": elapsed_ms,
                    "error": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    def _tables_snapshot_unlocked(self) -> list[dict]:
        items = []
        for name in self.database.tables():
            t = self.database.table(name)
            file_path = self.database.file(name)
            items.append({
                "name": name,
                "rows": len(t.rows),
                "columns": len(t.struct),
                "bytes": os.path.getsize(file_path) if os.path.isfile(file_path) else 0,
            })
        return items

    def tables_snapshot(self) -> list[dict]:
        with self.lock:
            return self._tables_snapshot_unlocked()

    def status_snapshot(self) -> dict:
        with self.lock:
            tables = self._tables_snapshot_unlocked()
            db_path = self.database.path
        with self._meta_lock:
            stats = dict(self._stats)
        host, port = self.server_address[:2]
        return {
            "status": "ok",
            "version": __version__,
            "started_at": self.started_iso,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "listen_address": f"http://{host}:{port}",
            "unix_socket": self.config.unix_socket,
            "admin_enabled": self.config.admin_enabled,
            "auth_required": bool(self.config.token),
            "database": {
                "path": db_path,
                "tables": len(tables),
                "total_rows": sum(t["rows"] for t in tables),
                "total_bytes": sum(t["bytes"] for t in tables),
            },
            "stats": stats,
            "process": process_metrics(),
        }

    def table_detail(self, name: str, limit: int = 50, offset: int = 0) -> dict:
        with self.lock:
            table = self.database.table(name)
            file_path = self.database.file(name)
            col_names = list(table.struct.column_names)
            schema = [{"name": col, "type": type_name(ty)} for col, ty in zip(col_names, table.struct.columns)]
            total_rows = len(table.rows)
            slice_rows = [[row.col(j).value for j in range(len(col_names))] for row in table.rows[offset:offset + limit]]
            return {
                "name": name,
                "columns": col_names,
                "schema": schema,
                "rows": slice_rows,
                "total_rows": total_rows,
                "limit": limit,
                "offset": offset,
                "bytes": os.path.getsize(file_path) if os.path.isfile(file_path) else 0,
                "cache": table.cache_info,
            }

    def history_snapshot(self) -> list[dict]:
        with self._meta_lock:
            return list(self._history)

    def clear_history(self) -> None:
        with self._meta_lock:
            self._history.clear()

    def metrics_text(self) -> str:
        status = self.status_snapshot()
        tables = self.tables_snapshot()
        stats = status["stats"]
        proc = status["process"]
        lines = [
            "# HELP siql_uptime_seconds Server uptime in seconds",
            "# TYPE siql_uptime_seconds gauge",
            f"siql_uptime_seconds {status['uptime_seconds']}",
            "# HELP siql_requests_total Total HTTP requests handled",
            "# TYPE siql_requests_total counter",
            f"siql_requests_total {stats['requests']}",
            "# HELP siql_queries_total Total query requests processed",
            "# TYPE siql_queries_total counter",
            f"siql_queries_total {stats['queries']}",
            "# HELP siql_query_errors_total Total failed query requests",
            "# TYPE siql_query_errors_total counter",
            f"siql_query_errors_total {stats['errors']}",
            "# HELP siql_tables_total Number of tables in database",
            "# TYPE siql_tables_total gauge",
            f"siql_tables_total {len(tables)}",
            "# HELP siql_table_rows Number of rows per table",
            "# TYPE siql_table_rows gauge",
        ]
        for t in tables:
            escaped = t["name"].replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'siql_table_rows{{table="{escaped}"}} {t["rows"]}')
        lines += [
            "# HELP siql_table_bytes Table file size in bytes",
            "# TYPE siql_table_bytes gauge",
        ]
        for t in tables:
            escaped = t["name"].replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'siql_table_bytes{{table="{escaped}"}} {t["bytes"]}')
        if proc.get("rss_bytes") is not None:
            lines += [
                "# HELP siql_process_resident_memory_bytes Resident memory size in bytes",
                "# TYPE siql_process_resident_memory_bytes gauge",
                f"siql_process_resident_memory_bytes {proc['rss_bytes']}",
            ]
        if proc.get("open_fds") is not None:
            lines += [
                "# HELP siql_process_open_fds Number of open file descriptors",
                "# TYPE siql_process_open_fds gauge",
                f"siql_process_open_fds {proc['open_fds']}",
            ]
        return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    server: SiQLServer
    protocol_version = "HTTP/1.1"
    server_version = f"SiQL/{__version__}"

    @property
    def _owner(self) -> SiQLServer:
        return getattr(self.server, "owner", self.server)

    def do_GET(self):
        self._dispatch_get()

    def do_POST(self):
        self._dispatch_post()

    def do_DELETE(self):
        self._dispatch_delete()

    def do_OPTIONS(self):
        owner = self._owner
        owner.count_request()
        if owner.config.cors_origin:
            self._send_raw(HTTPStatus.NO_CONTENT, b"", "text/plain", extra_headers={
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",
            })
        else:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed"})

    def _split_target(self) -> tuple[str, str]:
        path, _, qs = self.path.partition("?")
        return path, qs

    def _run_guarded(self, fn, *, auth: bool = True):
        owner = self._owner
        owner.count_request()
        try:
            if auth:
                self._authorize()
            result = fn()
            if result is not None:
                self._send(HTTPStatus.OK, result)
        except _Reject as e:
            self._send(e.status, e.payload)
        except Exception:
            log.exception("Error handling %s %s", self.command, self.path)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})

    def _dispatch_get(self):
        path, qs = self._split_target()
        owner = self._owner
        if path == "/":
            def _root():
                if not owner.config.admin_enabled:
                    raise _Reject(HTTPStatus.NOT_FOUND, "Not found")
                self._send_raw(HTTPStatus.FOUND, b"", "text/plain", extra_headers={"Location": "/admin"})
                return None
            self._run_guarded(_root, auth=False)
            return
        if path in ("/admin", "/admin/"):
            def _admin():
                if not owner.config.admin_enabled:
                    raise _Reject(HTTPStatus.NOT_FOUND, "Not found")
                self._send_raw(
                    HTTPStatus.OK,
                    ADMIN_HTML.encode("utf-8"),
                    "text/html; charset=utf-8",
                    extra_headers={
                        "Cache-Control": "no-store",
                        "X-Frame-Options": "SAMEORIGIN",
                        "Content-Security-Policy": (
                            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                            "connect-src 'self'"
                        ),
                    },
                )
                return None
            self._run_guarded(_admin, auth=False)
            return
        if path == "/health":
            self._run_guarded(self._health, auth=False)
            return
        if path == "/tables":
            self._run_guarded(self._tables)
            return
        if path == "/api/status":
            self._run_guarded(owner.status_snapshot)
            return
        if path == "/api/tables":
            self._run_guarded(lambda: {"tables": owner.tables_snapshot()})
            return
        if path == "/api/history":
            self._run_guarded(lambda: {"history": owner.history_snapshot()})
            return
        if path == "/metrics":
            def _metrics():
                self._send_raw(HTTPStatus.OK, owner.metrics_text().encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8")
                return None
            self._run_guarded(_metrics)
            return
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "tables" and parts[3]:
            name = urllib.parse.unquote(parts[3])
            self._run_guarded(lambda: self._table_detail(name, qs))
            return
        self._run_guarded(self._not_found, auth=False)

    def _dispatch_post(self):
        path, _ = self._split_target()
        if path in ("/query", "/api/query"):
            self._run_guarded(self._query)
            return
        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "tables" and parts[3] and parts[4] == "compact":
            name = urllib.parse.unquote(parts[3])
            self._run_guarded(lambda: self._compact_table(name))
            return
        self._run_guarded(self._not_found, auth=False)

    def _dispatch_delete(self):
        path, _ = self._split_target()
        owner = self._owner
        if path == "/api/history":
            def _clear():
                owner.clear_history()
                return {"cleared": True}
            self._run_guarded(_clear)
            return
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "tables" and parts[3]:
            name = urllib.parse.unquote(parts[3])
            self._run_guarded(lambda: self._drop_table(name))
            return
        self._run_guarded(self._not_found, auth=False)

    @staticmethod
    def _not_found():
        raise _Reject(HTTPStatus.NOT_FOUND, "Not found")

    def _authorize(self):
        token = self._owner.config.token
        if not token:
            return
        scheme, _, given = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), token.encode()):
            raise _Reject(HTTPStatus.UNAUTHORIZED, "Missing or invalid token")

    def _read_json(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            raise _Reject(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
        if int(length) > self._owner.config.max_body:
            raise _Reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        try:
            return json.loads(self.rfile.read(int(length)))
        except ValueError:
            raise _Reject(HTTPStatus.BAD_REQUEST, "Body must be JSON") from None

    def _health(self):
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": round(time.time() - self._owner.started_at, 3),
        }

    def _tables(self):
        owner = self._owner
        with owner.lock:
            return {"tables": owner.database.tables()}

    def _table_detail(self, name: str, qs: str):
        params = urllib.parse.parse_qs(qs, keep_blank_values=True)
        try:
            limit = int(params["limit"][0]) if "limit" in params else 50
            offset = int(params["offset"][0]) if "offset" in params else 0
        except ValueError:
            raise _Reject(HTTPStatus.BAD_REQUEST, "limit and offset must be integers") from None
        if limit < 0 or limit > 1000 or offset < 0:
            raise _Reject(HTTPStatus.BAD_REQUEST, "limit must be 0..1000 and offset >= 0")
        try:
            return self._owner.table_detail(name, limit=limit, offset=offset)
        except KeyError as e:
            raise _Reject(HTTPStatus.NOT_FOUND, e.args[0]) from None
        except ValueError as e:
            raise _Reject(HTTPStatus.BAD_REQUEST, e.args[0]) from None

    def _compact_table(self, name: str):
        owner = self._owner
        with owner.lock:
            try:
                t = owner.database.table(name)
                file_path = owner.database.file(name)
                before = os.path.getsize(file_path)
                t.compact()
                after = os.path.getsize(file_path)
            except KeyError as e:
                raise _Reject(HTTPStatus.NOT_FOUND, e.args[0]) from None
            except ValueError as e:
                raise _Reject(HTTPStatus.BAD_REQUEST, e.args[0]) from None
        return {"table": name, "bytes_before": before, "bytes_after": after}

    def _drop_table(self, name: str):
        owner = self._owner
        with owner.lock:
            try:
                owner.database.drop(name)
            except KeyError as e:
                raise _Reject(HTTPStatus.NOT_FOUND, e.args[0]) from None
            except ValueError as e:
                raise _Reject(HTTPStatus.BAD_REQUEST, e.args[0]) from None
        return {"deleted": name, "message": f"Dropped table {name}"}

    def _query(self):
        owner = self._owner
        body = self._read_json()
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, str):
            raise _Reject(HTTPStatus.BAD_REQUEST, 'Expected a JSON object with a "query" string')
        results = []
        t0 = time.monotonic()
        try:
            statements = parse(query)
            with owner.lock:
                for statement in statements:
                    results.append(owner.interpreter.run(statement).to_dict())
        except QueryError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000.0, 3)
            owner.record_query(query, False, len(results), elapsed_ms, str(e))
            raise _Reject(HTTPStatus.BAD_REQUEST, str(e), results=results, elapsed_ms=elapsed_ms) from None
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 3)
        owner.record_query(query, True, len(results), elapsed_ms)
        return {"results": results, "elapsed_ms": elapsed_ms}

    def _send(self, status: HTTPStatus, payload: dict):
        data = json.dumps(payload, default=repr).encode()
        self._send_raw(status, data, "application/json")

    def _send_raw(self, status: HTTPStatus, data: bytes, content_type: str, *, extra_headers: dict[str, str] | None = None):
        if status >= 400:
            self.close_connection = True  # the request body may not have been read
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._owner.config.cors_origin:
            self.send_header("Access-Control-Allow-Origin", self._owner.config.cors_origin)
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def address_string(self):
        if isinstance(self.client_address, tuple) and self.client_address and self.client_address[0]:
            return str(self.client_address[0])
        return "unix"

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def serve(config: ServerConfig, *, config_path: str | None = None):
    """Run the server until interrupted (supports SIGTERM, SIGINT, SIGHUP reload, and systemd sd_notify)."""
    server = SiQLServer(config)
    host, port = server.server_address[:2]
    if not config.token and not _is_loopback(config.host):
        log.warning("No token set: anyone who can reach %s:%s can read and change the tables", host, port)
    log.info("SiQL server listening on http://%s:%s (data: %s)", host, port, config.data_dir)
    if config.unix_socket:
        log.info("SiQL Unix socket listening on %s", config.unix_socket)
    if config.admin_enabled:
        log.info("SiQL Admin UI available at http://%s:%s/admin", host, port)

    stop_event = threading.Event()
    reload_event = threading.Event()
    previous_handlers: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        def _on_stop(_signum, _frame):
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _on_stop)
        if hasattr(signal, "SIGHUP"):
            def _on_reload(_signum, _frame):
                reload_event.set()

            previous_handlers[signal.SIGHUP] = signal.getsignal(signal.SIGHUP)
            signal.signal(signal.SIGHUP, _on_reload)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sd_notify(f"READY=1\nSTATUS=Listening on http://{host}:{port}")

    wd_interval = watchdog_interval()
    last_wd = time.monotonic()
    poll_timeout = min(wd_interval, 0.5) if wd_interval else 0.5

    try:
        while not stop_event.wait(timeout=poll_timeout):
            if reload_event.is_set():
                reload_event.clear()
                if config_path:
                    sd_notify("RELOADING=1")
                    try:
                        new_config = ServerConfig.load(config_path)
                        new_config.token = os.environ.get(TOKEN_ENV) or new_config.token
                        server.reload_config(new_config)
                        level = getattr(logging, new_config.log_level.upper(), None)
                        if isinstance(level, int):
                            logging.getLogger().setLevel(level)
                        log.info("Reloaded configuration from %s", config_path)
                    except Exception:
                        log.exception("Failed to reload configuration from %s", config_path)
                    sd_notify(f"READY=1\nSTATUS=Listening on http://{host}:{port}")
                else:
                    log.info("Received SIGHUP without a config file; nothing to reload")
            if wd_interval is not None and (time.monotonic() - last_wd) >= wd_interval:
                sd_notify("WATCHDOG=1")
                last_wd = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        sd_notify("STOPPING=1")
        server.shutdown()
        thread.join(timeout=5.0)
        with server.lock:
            pass
        server.server_close()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
