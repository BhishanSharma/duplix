"""The three input files: upload, list, remove.

The daily flight schedule is held in the server's memory for the
session. The two period rosters are also kept on disk (``roster_store``)
and reloaded at start-up: they are uploaded about once a month, several
can be held at once, and the one covering the allocation date is used.
Nothing is ever written back to the source workbooks.

Which slot a file appears in is decided by its cadence, not by this
module: the daily flight schedule sits on the dashboard and the two
period rosters sit in the Setup sidebar. See ``INPUT_CADENCE`` in
``state.py``. Every endpoint here answers with the same shape as
``GET /api/inputs`` so the UI has one payload to render from.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import date as date_t

from ...config import load_config
from ...io.roster_library import roster_dates
from ...state import INPUT_KINDS, SETUP_INPUT_KINDS, STATE
from .. import readback
from ..readback.common import CONFIG_PATH
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
        if kind in SETUP_INPUT_KINDS:
            return _upload_roster(kind, filename, data)
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

    @router.delete("/api/inputs/{kind}/{file_id}")
    def remove_one(req: Request) -> Response:
        """Drop one stored roster, keeping the others."""
        kind = req.params["kind"]
        if kind not in SETUP_INPUT_KINDS:
            return bad_request(f"{kind!r} does not hold multiple files")
        if not STATE.remove_roster(kind, req.params["file_id"]):
            return bad_request("no such roster")
        logger.info("[roster] removed %s %s", kind, req.params["file_id"])
        return json_response(readback.read_inputs(STATE))

    @router.get("/api/rosters/coverage")
    def coverage(req: Request) -> Response:
        """Does the stored roster set cover ``?date=`` (default: the
        session's run date, else the server's today) and the day after?"""
        raw = (req.query.get("date") or [""])[0]
        try:
            day = date_t.fromisoformat(raw) if raw else (
                STATE.run_date or date_t.today()
            )
        except ValueError:
            return bad_request(f"bad date {raw!r}; expected YYYY-MM-DD")
        return json_response(STATE.roster_coverage(day))


def _upload_roster(kind: str, filename: str, data: bytes) -> Response:
    """Validate a period roster at the door, then keep it.

    Parsing here means a wrong file (no ID column, duplicate ids, no
    date columns) is refused with the reader's own message right away,
    instead of surfacing as a failed Plan days later. The dates it
    covers are what later picks it for an allocation date.
    """
    try:
        dates = roster_dates(kind, data, load_config(CONFIG_PATH))
    except Exception as exc:  # noqa: BLE001 — reader errors are user-facing
        return bad_request(f"could not read {filename}: {exc}")
    if not dates:
        return bad_request(
            f"{filename} has no date columns (expected headers like "
            "'Mon,31Aug'). Is this the right file?"
        )
    rf = STATE.add_roster(kind, filename, data, dates)
    logger.info(
        "[upload] %s: %s (%d days, %s to %s)",
        kind, rf.filename, len(rf.dates), rf.start, rf.end,
    )
    return json_response({
        "uploaded": {"kind": kind, **rf.as_json()},
        **readback.read_inputs(STATE),
    })


__all__ = ["register"]
