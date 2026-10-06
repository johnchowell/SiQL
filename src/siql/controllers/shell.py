"""Command line: `python -m siql [-d DIR] [-f FILE] [QUERY ...]` (also installed as `siql`).

With a query or file it runs them and exits; with neither it reads piped input or starts an interactive shell.
"""
import argparse
import sys

from .interpreter import Interpreter
from .parser import QueryError, parse


def _run(interpreter: Interpreter, script: str) -> bool:
    try:
        for statement in parse(script):
            print(interpreter.run(statement))
    except QueryError as e:
        print(f"Error: {e}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="siql", description="Run SiQL queries against a folder of .siql table files.",
        epilog='example: python -m siql "CREATE TABLE people (name str, age int)"')
    parser.add_argument("query", nargs="*", help="statements to run, separated by ';' (quote them for your shell)")
    parser.add_argument("-d", "--data-dir", default=".", help="folder holding the table files (default: .)")
    parser.add_argument("-f", "--file", help="run the statements in FILE")
    args = parser.parse_args(argv)
    interpreter = Interpreter(args.data_dir)

    if args.file or args.query:
        if args.file:
            try:
                with open(args.file, encoding="utf-8") as f:
                    script = f.read()
            except OSError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            if not _run(interpreter, script):
                return 1
        return 0 if _run(interpreter, " ".join(args.query)) else 1
    if not sys.stdin.isatty():
        return 0 if _run(interpreter, sys.stdin.read()) else 1

    print(f"SiQL shell on {interpreter.database.path}. End statements with ';'. Type .exit to quit.")
    buffer: list[str] = []
    while True:
        try:
            line = input("  ... " if buffer else "siql> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            buffer.clear()
            continue
        if not buffer and line.strip() in (".exit", ".quit"):
            return 0
        buffer.append(line)
        if line.rstrip().endswith(";"):
            _run(interpreter, "\n".join(buffer))
            buffer.clear()
