"""Launcher for the Flight Allocation console.

State lives in the server process for the length of a session, so there
is nothing for a per-stage CLI command to read or write — the stages are
driven from the dashboard. This module exists to start the UI.

    python -m src.cli            # launch the console
    python -m src.cli --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import sys

from .web.server import DEFAULT_HOST, DEFAULT_PORT, serve


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="flight-alloc",
        description="Launch the Flight Allocation console (local web UI).",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"bind address (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"bind port (default {DEFAULT_PORT})")
    p.add_argument("--no-browser", action="store_true",
                   help="do not auto-open the browser")
    # Accepted and ignored so an old shortcut like `flight-alloc web`
    # still launches instead of erroring out.
    p.add_argument("command", nargs="?", default="web",
                   help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
