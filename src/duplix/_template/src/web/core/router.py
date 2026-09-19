"""A pattern -> handler table, so adding an endpoint is a registration
rather than another branch in a growing if/elif chain.

Patterns are literal paths with optional ``{name}`` segments:

    @router.get("/api/dashboard")
    def dashboard(req): ...

    @router.post("/api/inputs/{kind}")
    def upload(req): ...            # req.params["kind"]

    @router.get("/static/{path*}")
    def asset(req): ...             # trailing * spans "/" too

Literal routes are matched by dict lookup; only the handful of
parameterised ones fall through to the regex list, so dispatch cost
doesn't grow with the number of plain endpoints.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .responses import Response, not_found, server_error

#: A route handler. Takes the request, returns the response.
Handler = Callable[["Request"], Response]

#: ``{name}`` matches one path segment; ``{name*}`` matches the rest of
#: the path, slashes included.
_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(\*?)\}")


@dataclass(slots=True)
class Request:
    """One inbound request, already parsed enough for a handler to use.

    ``body`` is read by the request loop before dispatch so a handler
    never touches the socket — it is the raw bytes for an upload and
    the JSON source for everything else.
    """

    method: str
    path: str
    headers: Any = None
    body: bytes = b""
    params: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        """The body as a dict. A malformed or non-object body reads as
        ``{}`` — handlers validate the fields they need and return a
        400 naming the missing one, which is a better error than a
        generic parse failure."""
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def header(self, name: str, default: str = "") -> str:
        if self.headers is None:
            return default
        return self.headers.get(name, default) or default


def _compile(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    last = 0
    for m in _PARAM_RE.finditer(pattern):
        parts.append(re.escape(pattern[last:m.start()]))
        body = ".+" if m.group(2) else "[^/]+"
        parts.append(f"(?P<{m.group(1)}>{body})")
        last = m.end()
    parts.append(re.escape(pattern[last:]))
    return re.compile("^" + "".join(parts) + "$")


class Router:
    """Route table. One instance per server; built once at start-up."""

    def __init__(self) -> None:
        self._literal: dict[tuple[str, str], Handler] = {}
        self._patterned: list[tuple[str, re.Pattern[str], Handler]] = []

    # -- registration ------------------------------------------------

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        method = method.upper()
        if _PARAM_RE.search(pattern):
            self._patterned.append((method, _compile(pattern), handler))
        else:
            self._literal[(method, pattern)] = handler

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            self.add(method, pattern, fn)
            return fn
        return decorator

    def get(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route("GET", pattern)

    def post(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route("POST", pattern)

    def put(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route("PUT", pattern)

    def delete(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route("DELETE", pattern)

    # -- dispatch ----------------------------------------------------

    def resolve(self, method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
        handler = self._literal.get((method, path))
        if handler is not None:
            return handler, {}
        for route_method, regex, fn in self._patterned:
            if route_method != method:
                continue
            m = regex.match(path)
            if m:
                return fn, m.groupdict()
        return None

    def dispatch(self, method: str, raw_path: str, headers: Any, body: bytes) -> Response:
        """Find the handler for this request and run it.

        Any exception a handler lets escape becomes a 500 carrying the
        traceback, so one broken endpoint can't take the console down.
        """
        parsed = urllib.parse.urlparse(raw_path)
        match = self.resolve(method, parsed.path)
        if match is None:
            return not_found()
        handler, params = match
        request = Request(
            method=method,
            path=parsed.path,
            headers=headers,
            body=body,
            params=params,
            query=urllib.parse.parse_qs(parsed.query),
        )
        try:
            return handler(request)
        except Exception as exc:  # noqa: BLE001 — see docstring
            return server_error(exc, context=f"{method} {parsed.path}")

    @property
    def routes(self) -> list[str]:
        """``"GET /api/dashboard"`` for each registered route — handy in
        a log line at start-up and when checking a new blueprint
        actually registered."""
        literal = [f"{m} {p}" for (m, p) in self._literal]
        patterned = [f"{m} {r.pattern}" for (m, r, _) in self._patterned]
        return sorted(literal + patterned)


__all__ = ["Handler", "Request", "Router"]
