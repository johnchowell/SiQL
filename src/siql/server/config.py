import os
import tomllib
from dataclasses import dataclass, fields
from typing import get_type_hints

DEFAULT_PORT = 8642
TOKEN_ENV = "SIQL_TOKEN"

TEMPLATE = f"""\
# SiQL server configuration
[server]
host = "127.0.0.1"   # "0.0.0.0" accepts connections from other machines (set a token first)
port = {DEFAULT_PORT}
data_dir = "data"    # folder holding the .siql table files, relative to this file
# token = "..."      # require "Authorization: Bearer <token>"; the {TOKEN_ENV} environment variable overrides this
max_body = 1048576   # largest accepted request body, in bytes
log_level = "INFO"
# unix_socket = "/run/siql/siql.sock"  # optional Linux Unix domain socket listener
# admin_enabled = true                 # serve the built-in admin web UI at /admin
# history_limit = 100                  # number of recent queries retained for /api/history
# cors_origin = "*"                    # optional Access-Control-Allow-Origin header
"""


@dataclass
class ServerConfig():
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    data_dir: str = "data"
    token: str | None = None
    max_body: int = 1024 * 1024
    log_level: str = "INFO"
    unix_socket: str | None = None
    admin_enabled: bool = True
    history_limit: int = 100
    cors_origin: str | None = None

    @classmethod
    def load(cls, path: str) -> "ServerConfig":
        """Read the `[server]` table of a TOML file. Relative paths resolve from the file's folder."""
        with open(path, "rb") as f:
            settings = tomllib.load(f).get("server", {})
        if not isinstance(settings, dict):
            raise ValueError(f"[server] in {path} must be a table")
        hints = get_type_hints(cls)
        types = {
            f.name: (bool if hints[f.name] is bool else int if hints[f.name] is int else str)
            for f in fields(cls)
        }
        for key, value in settings.items():
            if key not in types:
                raise ValueError(f"Unknown setting in {path}: {key}")
            expected = types[key]
            if expected is bool:
                if not isinstance(value, bool):
                    raise ValueError(f"Setting {key} in {path} must be a boolean")
            elif expected is int:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"Setting {key} in {path} must be a number")
            elif not isinstance(value, str):
                raise ValueError(f"Setting {key} in {path} must be a string")
        config = cls(**settings)
        base = os.path.dirname(os.path.abspath(path))
        config.data_dir = os.path.join(base, config.data_dir)
        if config.unix_socket:
            config.unix_socket = os.path.join(base, config.unix_socket)
        return config

    def __post_init__(self):
        if not 0 <= self.port <= 65535:
            raise ValueError(f"Invalid port: {self.port}")
        if self.max_body < 0:
            raise ValueError(f"Invalid max_body: {self.max_body}")
        if self.history_limit < 0:
            raise ValueError(f"Invalid history_limit: {self.history_limit}")
        if self.unix_socket == "":
            self.unix_socket = None
        if self.cors_origin == "":
            self.cors_origin = None
