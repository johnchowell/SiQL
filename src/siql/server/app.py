"""HTTP + JSON API for a SiQL Database.

    GET  /health                    -> {"status": "ok", "version": "..."}   (no token needed)
    GET  /tables                    -> {"tables": ["people", ...]}
    POST /query  {"query": "..."}   -> {"results": [{"columns", "rows", "rowcount", "message"}, ...]}

Errors return {"error": "..."}. A failed query also returns the results of the statements before it,
which were already saved.
"""
import hmac
import ipaddress
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__
from ..controllers import Interpreter, QueryError, parse
from ..managers import Database
from .config import ServerConfig

log = logging.getLogger(__package__)


class _Reject(Exception):
    def __init__(self, status: HTTPStatus, message: str, **extra):
        super().__init__(message)
        self.status, self.payload = status, {"error": message, **extra}


class SiQLServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: ServerConfig):
        self.config = config
        self.database = Database(config.data_dir)
        self.interpreter = Interpreter(self.database, file_access=False)
        self.lock = threading.Lock()  # tables aren't thread-safe, so requests use them one at a time
        super().__init__((config.host, config.port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: SiQLServer
    protocol_version = "HTTP/1.1"
    server_version = f"SiQL/{__version__}"

    def do_GET(self):
        self._handle({"/health": self._health, "/tables": self._tables})

    def do_POST(self):
        self._handle({"/query": self._query})

    def _handle(self, routes):
        try:
            route = routes.get(self.path.split("?", 1)[0])
            if route is None:
                raise _Reject(HTTPStatus.NOT_FOUND, "Not found")
            if route != self._health:
                self._authorize()
            self._send(HTTPStatus.OK, route())
        except _Reject as e:
            self._send(e.status, e.payload)
        except Exception:
            log.exception("Error handling %s %s", self.command, self.path)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})

    def _authorize(self):
        token = self.server.config.token
        if not token:
            return
        scheme, _, given = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), token.encode()):
            raise _Reject(HTTPStatus.UNAUTHORIZED, "Missing or invalid token")

    def _read_json(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            raise _Reject(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
        if int(length) > self.server.config.max_body:
            raise _Reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        try:
            return json.loads(self.rfile.read(int(length)))
        except ValueError:
            raise _Reject(HTTPStatus.BAD_REQUEST, "Body must be JSON") from None

    def _health(self):
        return {"status": "ok", "version": __version__}

    def _tables(self):
        with self.server.lock:
            return {"tables": self.server.database.tables()}

    def _query(self):
        body = self._read_json()
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, str):
            raise _Reject(HTTPStatus.BAD_REQUEST, 'Expected a JSON object with a "query" string')
        results = []
        try:
            statements = parse(query)
            with self.server.lock:
                for statement in statements:
                    results.append(self.server.interpreter.run(statement).to_dict())
        except QueryError as e:
            raise _Reject(HTTPStatus.BAD_REQUEST, str(e), results=results) from None
        return {"results": results}

    def _send(self, status: HTTPStatus, payload: dict):
        data = json.dumps(payload, default=repr).encode()
        if status >= 400:
            self.close_connection = True  # the request body may not have been read
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def serve(config: ServerConfig):
    """Run the server until interrupted."""
    server = SiQLServer(config)
    host, port = server.server_address[:2]
    if not config.token and not _is_loopback(config.host):
        log.warning("No token set: anyone who can reach %s:%s can read and change the tables", host, port)
    log.info("SiQL server listening on http://%s:%s (data: %s)", host, port, config.data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
