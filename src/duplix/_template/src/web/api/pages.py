"""The page itself: index.html and everything under ``static/``.

Registered last, and the wildcard is scoped to ``/static/`` rather than
``/``, so an unmatched ``/api/...`` path still returns a JSON 404
instead of being answered with HTML.
"""

from __future__ import annotations

from ..core import static_files
from ..core.responses import Response
from ..core.router import Request, Router


def register(router: Router) -> None:

    @router.get("/")
    def index(_req: Request) -> Response:
        return static_files.serve(static_files.INDEX)

    @router.get("/index.html")
    def index_html(_req: Request) -> Response:
        return static_files.serve(static_files.INDEX)

    @router.get("/static/{path*}")
    def asset(req: Request) -> Response:
        return static_files.serve(req.params["path"])


__all__ = ["register"]
