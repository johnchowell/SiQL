import os
import re
import subprocess
import sys

from .config import TOKEN_ENV

_UNIT_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _quote(arg: str) -> str:
    """Quote an argument for a systemd command line (which also expands % and $)."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if any(c.isspace() or c in "\"'\\" for c in arg):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def systemd_unit(config_path: str, *, python: str | None = None, user: str | None = None,
                 description: str = "SiQL server", notify: bool = True,
                 watchdog_sec: int = 30, runtime_dir: str | None = "siql") -> str:
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
        f"Type={'notify' if notify else 'simple'}",
        f"ExecStart={' '.join(_quote(a) for a in command)}",
        "ExecReload=/bin/kill -HUP $MAINPID",
        f"WorkingDirectory={os.path.dirname(config_path)}",
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=15",
        "Environment=PYTHONUNBUFFERED=1",
        f"# Keep the token out of this file: put {TOKEN_ENV}=... in a root-only file and uncomment",
        "# EnvironmentFile=/etc/siql/siql.env",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=full",
    ]
    if notify and watchdog_sec > 0:
        lines.append(f"WatchdogSec={watchdog_sec}s")
    if runtime_dir:
        lines.append(f"RuntimeDirectory={runtime_dir}")
        lines.append("RuntimeDirectoryMode=0750")
    if user:
        lines.append(f"User={user}")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)


def install_systemd_unit(config_path: str, *, name: str = "siql", python: str | None = None,
                         user: str | None = None, description: str = "SiQL server",
                         notify: bool = True, unit_dir: str = "/etc/systemd/system",
                         enable: bool = False) -> str:
    """Write a systemd service unit file and optionally daemon-reload + enable --now it."""
    if not os.path.isfile(config_path):
        raise ValueError(f"No config file at {config_path}; create one with: siql-server init")
    service_name = name[:-8] if name.endswith(".service") else name
    if not service_name or not _UNIT_NAME.match(service_name):
        raise ValueError(f"Invalid systemd unit name: {name!r}")
    text = systemd_unit(config_path, python=python, user=user, description=description, notify=notify)
    os.makedirs(unit_dir, exist_ok=True)
    unit_path = os.path.join(unit_dir, f"{service_name}.service")
    with open(unit_path, "w", encoding="utf-8") as f:
        f.write(text)
    if enable:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", service_name], check=True)
    return unit_path
