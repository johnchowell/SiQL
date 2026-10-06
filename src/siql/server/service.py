import os
import sys

from .config import TOKEN_ENV


def _quote(arg: str) -> str:
    """Quote an argument for a systemd command line (which also expands % and $)."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if any(c.isspace() or c in "\"'\\" for c in arg):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def systemd_unit(config_path: str, *, python: str | None = None, user: str | None = None,
                 description: str = "SiQL server") -> str:
    """Text of a systemd service unit that runs the server with the given config file."""
    config_path = os.path.abspath(config_path)
    command = [python or sys.executable, "-m", __package__, "serve", "--config", config_path]
    lines = [
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={' '.join(_quote(a) for a in command)}",
        f"WorkingDirectory={os.path.dirname(config_path)}",
        "Restart=on-failure",
        "Environment=PYTHONUNBUFFERED=1",
        f"# Keep the token out of this file: put {TOKEN_ENV}=... in a root-only file and uncomment",
        "# EnvironmentFile=/etc/siql/siql.env",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=full",
    ]
    if user:
        lines.append(f"User={user}")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)
