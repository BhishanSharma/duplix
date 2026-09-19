"""Serve the ``static/`` tree.

The front end is a directory of ES modules and CSS partials rather than
two flat files, so this resolves an arbitrary relative path — but only
inside STATIC_DIR, and only for an allow-listed extension. A path that
resolves outside the tree (``../``, an absolute path, a symlink out) is
a 404, not a read.

Every response carries ``Cache-Control: no-store`` (set by the request
loop), and index.html gets the tree's newest mtime stamped onto its
entry-point URLs, so the browser always pulls current code after an
edit even if a proxy ignores the header.
"""

from __future__ import annotations

import re
from pathlib import Path

from .responses import Response, not_found

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

INDEX = "index.html"

#: Extension -> Content-Type. An extension absent from this map is not
#: servable, which is the allow-list.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

#: URLs in index.html that get a ?v= cache-buster stamped on.
_VERSIONED = re.compile(r'((?:href|src)=")(/static/[^"?]+\.(?:css|js))(")')


def _tree_version() -> int:
    """Newest mtime across the served tree. One number for the whole
    front end: a change to any module busts the entry points, which is
    what the browser actually re-requests."""
    newest = 0
    for path in STATIC_DIR.rglob("*"):
        if path.suffix in CONTENT_TYPES and path.is_file():
            newest = max(newest, int(path.stat().st_mtime))
    return newest


def _resolve(rel: str) -> Path | None:
    """Map a URL path to a file inside STATIC_DIR, or None if it escapes
    the tree, has a non-servable extension, or doesn't exist."""
    rel = rel.lstrip("/")
    if not rel:
        rel = INDEX
    candidate = (STATIC_DIR / rel).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None          # traversal attempt — outside the tree
    if candidate.suffix not in CONTENT_TYPES or not candidate.is_file():
        return None
    return candidate


def serve(rel_path: str) -> Response:
    """Read one file out of ``static/`` and wrap it in a Response."""
    path = _resolve(rel_path)
    if path is None:
        return not_found()
    body = path.read_bytes()
    if path.name == INDEX:
        version = _tree_version()
        text = _VERSIONED.sub(rf"\g<1>\g<2>?v={version}\g<3>", body.decode("utf-8"))
        body = text.encode("utf-8")
    return Response(status=200, body=body, content_type=CONTENT_TYPES[path.suffix])


__all__ = ["CONTENT_TYPES", "INDEX", "STATIC_DIR", "serve"]
