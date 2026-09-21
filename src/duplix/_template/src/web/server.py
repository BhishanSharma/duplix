"""The request loop and the launcher.

Everything about *what* an endpoint does lives in ``api``; this module
only knows how to get bytes off the socket, hand them to the router,
and put the Response back on the wire. Adding an endpoint never touches
this file.

Served from stdlib http.server — no FastAPI / Flask / external deps, so
it fits IT allow-list constraints and ships in the same PyInstaller
bundle as the engine.
"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .api import build_router
from .core.responses import Response, bad_request
from .core.router import Router

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Guard against an accidental multi-hundred-MB POST filling memory.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

logger = logging.getLogger("src.web")

#: Built once at import; every request thread reads from it.
ROUTER: Router = build_router()


class Handler(BaseHTTPRequestHandler):
    """One instance per request. Holds no state of its own — the shared
    state is the module-level ``STATE`` singleton the handlers reach."""

    server_version = "FlightAllocWeb/2.0"

    # HTTP/1.1 keep-alive. The default HTTP/1.0 + Connection: close
    # means a new TCP connection per request, and on Windows the
    # loopback handshake (plus Defender's inspection of it) is slow
    # enough that 5-6 cold connections on page load are noticeable.
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # DEBUG, not INFO: the console handler is set to INFO so the
        # operator sees what happened ([upload], [reset], ...) without a
        # line per request. On Windows every console write goes through
        # the console driver, and one per request bottlenecks the loop.
        logger.debug("%s - %s", self.client_address[0], format % args)

    def address_string(self) -> str:
        # The default can do a reverse DNS lookup on the client IP,
        # which on some Windows setups adds 30s+ per request.
        return self.client_address[0]

    # ------------ the four verbs, all the same shape ------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    # ------------ plumbing ------------

    def _handle(self, method: str) -> None:
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send(bad_request(str(exc)))
            return
        self._send(ROUTER.dispatch(method, self.path, self.headers, body))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return b""
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"payload too large ({length / 1e6:.0f} MB); "
                f"limit is {MAX_UPLOAD_BYTES / 1e6:.0f} MB"
            )
        return self.rfile.read(length)

    def _send(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Browser bailed mid-response — common when the operator
            # navigates or hits Reset while a poll is in flight. Don't
            # flood stderr with the stack trace.
            pass


class WebServer(ThreadingHTTPServer):
    """Threading so one slow request (a Reset, an in-flight poll) doesn't
    block the others — a single page load fires 5-6 API calls in
    parallel. ``daemon_threads`` so a hung handler doesn't keep the
    process alive after Ctrl+C."""

    daemon_threads = True


def configure_logging(level: int = logging.INFO) -> None:
    """Send this package's log to stdout.

    The operational lines ([upload], [reset], [staged-form], ...) are the
    only feedback an operator gets in the launcher window, so they need a
    handler. Per-request access logs sit at DEBUG and stay off.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = True,
) -> None:
    """Run the server in the foreground. Ctrl+C exits cleanly."""
    configure_logging()
    from ..state import STATE
    restored = STATE.load_persisted_rosters()
    if restored:
        logger.info("[rosters] restored %d saved roster file(s)", restored)
    server = WebServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Flight Allocation console - {url}")
    print("  Upload today's flight schedule from the dashboard;")
    print("  the two roster files live in the Setup sidebar and are")
    print("  remembered between runs (saved under data/rosters/).")
    print("  Ctrl+C to stop.")
    logger.debug("routes: %s", ", ".join(ROUTER.routes))

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="flight-alloc-web")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true",
                   help="do not auto-open the browser")
    args = p.parse_args(argv)
    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
