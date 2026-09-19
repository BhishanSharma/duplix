"""The three input files: upload, list, remove.

Files are held in the server's memory for the session — nothing is read
off disk and nothing is written back to the source workbooks, so a
fresh start is just a fresh upload.

Which slot a file appears in is decided by its cadence, not by this
module: the daily flight schedule sits on the dashboard and the two
period rosters sit in the Setup sidebar. See ``INPUT_CADENCE`` in
``state.py``. Every endpoint here answers with the same shape as
``GET /api/inputs`` so the UI has one payload to render from.
"""

from __future__ import annotations

import logging
import urllib.parse

from ...state import INPUT_KINDS, STATE
from .. import readback
from ..core.responses import Response, bad_request, json_response
from ..core.router import Request, Router

logger = logging.getLogger("src.web")


def register(router: Router) -> None:

    @router.post("/api/inputs/{kind}")
    def upload(req: Request) -> Response:
        kind = req.params["kind"]
        if kind not in INPUT_KINDS:
            return bad_request(
                f"unknown input {kind!r}; expected one of {list(INPUT_KINDS)}"
            )
        data = req.body
        if not data:
            return bad_request("empty upload")
        # An .xlsx is a zip; anything else would blow up deep inside
        # openpyxl on the next run, so reject it at the door.
        if not data.startswith(b"PK"):
            return bad_request(
                "that doesn't look like an .xlsx file — re-save it as "
                "Excel Workbook (.xlsx) and upload again"
            )
        filename = urllib.parse.unquote(
            req.header("X-Filename") or f"{kind}.xlsx"
        )
        up = STATE.set_input(kind, filename, data)
        logger.info("[upload] %s: %s (%.0f KB)", kind, up.filename, len(data) / 1024)
        return json_response({
            "uploaded": up.as_json(),
            **readback.read_inputs(STATE),
        })

    @router.delete("/api/inputs/{kind}")
    def remove(req: Request) -> Response:
        STATE.clear_input(req.params["kind"])
        return json_response(readback.read_inputs(STATE))


__all__ = ["register"]
