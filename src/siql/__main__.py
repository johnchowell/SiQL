"""Command line: python -m siql [-d DIR] [-f FILE] [QUERY ...]"""
from .controllers.shell import main

if __name__ == "__main__":
    raise SystemExit(main())
