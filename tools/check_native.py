"""Fail if any installed implementation is interpreted instead of native."""
from importlib import import_module
from importlib.machinery import ExtensionFileLoader


MODULES = (
    "_speedups", "_demo",
    "controllers.interpreter", "controllers.parser", "controllers.shell",
    "helpers.error_handler", "helpers.format", "helpers.search", "helpers.types",
    "managers.database", "managers.table_file",
    "models.diff", "models.row", "models.table", "models.tree",
    "server.app", "server.cli", "server.config", "server.service",
)


def main():
    for name in MODULES:
        module = import_module(f"siql.{name}")
        if not isinstance(module.__loader__, ExtensionFileLoader):
            raise RuntimeError(f"{module.__name__} is not native: {module.__file__}")
        print(f"{module.__name__}: {module.__file__}")
    print(f"Verified {len(MODULES)} native extensions")


if __name__ == "__main__":
    main()
