"""Command line: `siql-server serve|init|systemd` or `python -m siql.server ...`."""
import argparse
import logging
import os
import sys

from .config import ServerConfig, TEMPLATE, TOKEN_ENV
from .service import systemd_unit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="siql-server", description="Serve SiQL tables over HTTP.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the server")
    serve.add_argument("-c", "--config", help="TOML config file (default: built-in settings)")
    serve.add_argument("--host", help="address to listen on")
    serve.add_argument("--port", type=int, help="port to listen on")
    serve.add_argument("--data-dir", help="folder holding the .siql table files")

    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("-c", "--config", default="siql.toml", help="file to write (default: siql.toml)")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")

    unit = sub.add_parser("systemd", help="print a systemd unit for running the server as a service")
    unit.add_argument("-c", "--config", default="siql.toml", help="config file the service uses (default: siql.toml)")
    unit.add_argument("--user", help="user the service runs as")
    unit.add_argument("-o", "--output", help="write the unit to this file instead of printing it")

    args = parser.parse_args(argv)
    try:
        return {"serve": _serve, "init": _init, "systemd": _systemd}[args.command](args)
    except (OSError, ValueError) as e:
        print(f"siql-server: {e}", file=sys.stderr)
        return 1


def _serve(args) -> int:
    config = ServerConfig.load(args.config) if args.config else ServerConfig()
    for name in ("host", "port", "data_dir"):
        if getattr(args, name) is not None:
            setattr(config, name, getattr(args, name))
    config.token = os.environ.get(TOKEN_ENV) or config.token
    logging.basicConfig(level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")

    from .app import serve
    serve(config)
    return 0


def _init(args) -> int:
    with open(args.config, "w" if args.force else "x", encoding="utf-8") as f:
        f.write(TEMPLATE)
    print(f"Wrote {args.config}")
    return 0


def _systemd(args) -> int:
    if not os.path.isfile(args.config):
        raise ValueError(f"No config file at {args.config}; create one with: siql-server init")
    text = systemd_unit(args.config, user=args.user)
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
