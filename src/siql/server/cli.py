"""Command line: `siql-server serve|init|systemd|install|status|query` or `python -m siql.server ...`."""
import argparse
import json
import logging
import os
import subprocess
import sys

from ..helpers.format import box
from .config import ServerConfig, TEMPLATE, TOKEN_ENV, DEFAULT_PORT
from .runtime import http_request
from .service import systemd_unit, install_systemd_unit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="siql-server", description="Serve SiQL tables over HTTP.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the server")
    serve.add_argument("-c", "--config", help="TOML config file (default: built-in settings)")
    serve.add_argument("--host", help="address to listen on")
    serve.add_argument("--port", type=int, help="port to listen on")
    serve.add_argument("--data-dir", help="folder holding the .siql table files")
    serve.add_argument("--unix-socket", help="path to an additional Unix domain socket listener")
    serve.add_argument("--no-admin", action="store_true", help="disable the /admin web interface")

    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("-c", "--config", default="siql.toml", help="file to write (default: siql.toml)")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")

    unit = sub.add_parser("systemd", help="print a systemd unit for running the server as a service")
    unit.add_argument("-c", "--config", default="siql.toml", help="config file the service uses (default: siql.toml)")
    unit.add_argument("--user", help="user the service runs as")
    unit.add_argument("--simple", action="store_true", help="emit Type=simple instead of Type=notify")
    unit.add_argument("-o", "--output", help="write the unit to this file instead of printing it")

    install = sub.add_parser("install", help="install a systemd unit file for the server")
    install.add_argument("-c", "--config", default="siql.toml", help="config file the service uses (default: siql.toml)")
    install.add_argument("--name", default="siql", help="systemd service name (default: siql)")
    install.add_argument("--user", help="user the service runs as")
    install.add_argument("--unit-dir", default="/etc/systemd/system", help="systemd unit folder")
    install.add_argument("--simple", action="store_true", help="emit Type=simple instead of Type=notify")
    install.add_argument("--enable", action="store_true", help="run systemctl daemon-reload and enable --now")

    status = sub.add_parser("status", help="fetch runtime status from a running SiQL server")
    _add_client_args(status)
    status.add_argument("--json", action="store_true", help="print raw JSON status")

    query = sub.add_parser("query", help="run SiQL statements against a running SiQL server")
    query.add_argument("sql", help="SQL statements to execute")
    _add_client_args(query)
    query.add_argument("--json", action="store_true", help="print raw JSON response")

    args = parser.parse_args(argv)
    try:
        return {
            "serve": _serve,
            "init": _init,
            "systemd": _systemd,
            "install": _install,
            "status": _status,
            "query": _query,
        }[args.command](args)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"siql-server: {e}", file=sys.stderr)
        return 1


def _add_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=f"http://127.0.0.1:{DEFAULT_PORT}", help="server base URL")
    parser.add_argument("--unix-socket", help="connect to a Unix domain socket instead of TCP")
    parser.add_argument("--token", help=f"Bearer token (defaults to {TOKEN_ENV} env var)")


def _serve(args) -> int:
    config = ServerConfig.load(args.config) if args.config else ServerConfig()
    for name in ("host", "port", "data_dir", "unix_socket"):
        if getattr(args, name) is not None:
            setattr(config, name, getattr(args, name))
    if args.no_admin:
        config.admin_enabled = False
    config.token = os.environ.get(TOKEN_ENV) or config.token
    logging.basicConfig(level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")

    from .app import serve
    serve(config, config_path=args.config)
    return 0


def _init(args) -> int:
    with open(args.config, "w" if args.force else "x", encoding="utf-8") as f:
        f.write(TEMPLATE)
    print(f"Wrote {args.config}")
    return 0


def _systemd(args) -> int:
    if not os.path.isfile(args.config):
        raise ValueError(f"No config file at {args.config}; create one with: siql-server init")
    text = systemd_unit(args.config, user=args.user, notify=not args.simple)
    if not args.output:
        print(text, end="")
        return 0
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(text)
    name = os.path.basename(args.output)
    print(f"Wrote {args.output}. Install it with:\n"
          f"  sudo cp {args.output} /etc/systemd/system/{name}\n"
          f"  sudo systemctl daemon-reload && sudo systemctl enable --now {os.path.splitext(name)[0]}")
    return 0


def _install(args) -> int:
    unit_path = install_systemd_unit(
        args.config,
        name=args.name,
        user=args.user,
        notify=not args.simple,
        unit_dir=args.unit_dir,
        enable=args.enable,
    )
    unit_name = os.path.splitext(os.path.basename(unit_path))[0]
    if args.enable:
        print(f"Installed and enabled {unit_path}")
    else:
        print(f"Wrote {unit_path}. Enable it with:\n"
              f"  sudo systemctl daemon-reload && sudo systemctl enable --now {unit_name}")
    return 0


def _status(args) -> int:
    token = args.token or os.environ.get(TOKEN_ENV) or None
    code, payload = http_request(args.url, "/api/status", token=token, unix_socket=args.unix_socket)
    if code != 200 or not isinstance(payload, dict):
        msg = payload.get("error", payload) if isinstance(payload, dict) else payload
        print(f"siql-server status error ({code}): {msg}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    proc = payload.get("process", {})
    db = payload.get("database", {})
    stats = payload.get("stats", {})
    print(f"SiQL {payload.get('version')} ({payload.get('status')})")
    print(f"  listen:   {payload.get('listen_address')}" +
          (f" (unix: {payload.get('unix_socket')})" if payload.get("unix_socket") else ""))
    print(f"  data_dir: {db.get('path')} ({db.get('tables')} tables, {db.get('total_rows')} rows)")
    print(f"  process:  pid={proc.get('pid')} uptime={payload.get('uptime_seconds')}s rss={proc.get('rss_bytes')}")
    print(f"  stats:    requests={stats.get('requests')} queries={stats.get('queries')} errors={stats.get('errors')}")
    return 0


def _query(args) -> int:
    token = args.token or os.environ.get(TOKEN_ENV) or None
    code, payload = http_request(
        args.url, "/query", method="POST", body={"query": args.sql}, token=token, unix_socket=args.unix_socket
    )
    if args.json:
        print(json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else payload)
        return 0 if code == 200 else 1
    if isinstance(payload, dict):
        for res in payload.get("results", []):
            if res.get("columns"):
                print(box(res["columns"], [[str(v) for v in row] for row in res.get("rows", [])]))
            elif res.get("message"):
                print(res["message"])
        if code != 200:
            print(f"Error: {payload.get('error', code)}", file=sys.stderr)
            return 1
        return 0
    print(f"siql-server query error ({code}): {payload}", file=sys.stderr)
    return 1
