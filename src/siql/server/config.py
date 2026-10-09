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
"""


@dataclass
class ServerConfig():
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    data_dir: str = "data"
    token: str | None = None
    max_body: int = 1024 * 1024
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: str) -> "ServerConfig":
        """Read the `[server]` table of a TOML file. A relative `data_dir` is resolved from the file's folder."""
        with open(path, "rb") as f:
            settings = tomllib.load(f).get("server", {})
        hints = get_type_hints(cls)
        types = {f.name: (int if hints[f.name] is int else str) for f in fields(cls)}
        for key, value in settings.items():
            if key not in types:
                raise ValueError(f"Unknown setting in {path}: {key}")
            if not isinstance(value, types[key]) or isinstance(value, bool):
                raise ValueError(f"Setting {key} in {path} must be {'a number' if types[key] is int else 'a string'}")
        config = cls(**settings)
        config.data_dir = os.path.join(os.path.dirname(os.path.abspath(path)), config.data_dir)
        return config

    def __post_init__(self):
        if not 0 <= self.port <= 65535:
            raise ValueError(f"Invalid port: {self.port}")
        if self.max_body < 0:
            raise ValueError(f"Invalid max_body: {self.max_body}")
