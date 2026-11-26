"""Linux system runtime helpers: systemd sd_notify, /proc metrics, and Unix/HTTP client calls."""
import http.client
import json
import os
import socket
import sys
import urllib.parse


def sd_notify(state: str) -> bool:
    """Send a systemd readiness/watchdog/status notification via NOTIFY_SOCKET."""
    raw = os.environ.get("NOTIFY_SOCKET")
    if not raw or not hasattr(socket, "AF_UNIX"):
        return False
    addr = "\0" + raw[1:] if raw.startswith("@") else raw
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.sendto(state.encode("utf-8"), addr)
        return True
    except OSError:
        return False


def watchdog_interval() -> float | None:
    """Half the systemd WatchdogSec interval in seconds, or None if not enabled for this PID."""
    raw_usec = os.environ.get("WATCHDOG_USEC")
    if not raw_usec:
        return None
    raw_pid = os.environ.get("WATCHDOG_PID")
    if raw_pid:
        try:
            if int(raw_pid) != os.getpid():
                return None
        except ValueError:
            return None
    try:
        usec = int(raw_usec)
    except ValueError:
        return None
    if usec <= 0:
        return None
    return (usec / 1_000_000.0) / 2.0


def process_metrics() -> dict:
    """Best-effort Linux /proc/self runtime metrics (returns None for unavailable fields)."""
    metrics: dict = {
        "pid": os.getpid(),
        "platform": sys.platform,
        "rss_bytes": None,
        "vms_bytes": None,
        "threads": None,
        "open_fds": None,
    }
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    metrics["rss_bytes"] = int(line.split()[1]) * 1024
                elif line.startswith("VmSize:"):
                    metrics["vms_bytes"] = int(line.split()[1]) * 1024
                elif line.startswith("Threads:"):
                    metrics["threads"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        metrics["open_fds"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    return metrics


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def http_request(
    url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    unix_socket: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict | str]:
    """Send an HTTP/JSON request over TCP or a Unix domain socket and decode the response."""
    headers: dict[str, str] = {}
    payload: bytes | None = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if unix_socket:
        conn: http.client.HTTPConnection = _UnixHTTPConnection(unix_socket, timeout=timeout)
        target = path
    else:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(parsed.netloc, timeout=timeout)
        base_path = parsed.path.rstrip("/")
        target = f"{base_path}{path}" if base_path else path

    try:
        conn.request(method, target, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        content_type = resp.getheader("Content-Type", "")
        text = raw.decode("utf-8", errors="replace")
        if content_type.split(";", 1)[0].strip().lower() == "application/json":
            try:
                return resp.status, json.loads(text)
            except ValueError:
                return resp.status, text
        return resp.status, text
    finally:
        conn.close()
