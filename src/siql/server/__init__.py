"""Optional HTTP server for SiQL tables. Not imported by `import siql`; import `siql.server` to use it."""
from .config import ServerConfig
from .app import SiQLServer, serve
from .runtime import sd_notify
from .service import systemd_unit, install_systemd_unit

__all__ = ["ServerConfig", "SiQLServer", "serve", "sd_notify", "systemd_unit", "install_systemd_unit"]
