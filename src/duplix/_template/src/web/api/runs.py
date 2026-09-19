"""Starting a Plan / Allocate / Reset, and polling what's in flight.

A run is kicked off on a worker thread and the UI polls
``/api/run/status`` — the POST returns as soon as the run is accepted,
so a slow solve never holds a request open.
"""

from __future__ import annotations

import logging
from datetime import date as date_t

from ...state import STATE
from .. import runner
from ..core.responses import Response, bad_request, conflict, json_response
from ..core.router import Request, Router

logger = logging.getLogger("src.web")

#: Sub-commands ``/api/run`` will accept.
RUN_STEPS = {"all", "plan", "step1", "step2", "step3", "reset"}


def parse_date(raw: str | None) -> date_t | None:
    if not raw:
        return None
    try:
        return date_t.fromisoformat(raw)
    except ValueError:
        return None


def register(router: Router) -> None:

    @router.get("/api/run/status")
    def status(_req: Request) -> Response:
        return json_response(runner.status())

    @router.post("/api/run")
    def run(req: Request) -> Response:
        body = req.json()
        step = body.get("step") if isinstance(body.get("step"), str) else "all"
        if step not in RUN_STEPS:
            return bad_request(f"step={step!r} not allowed")
        if step == "reset":
            return _reset()

        d_day = parse_date(body.get("date"))
        if d_day is None:
            return bad_request("body.date required (YYYY-MM-DD)")

        missing = STATE.missing_inputs()
        if missing:
            from ...state import INPUT_LABELS
            names = ", ".join(INPUT_LABELS[k] for k in missing)
            return conflict({
                "error": f"upload the missing input file(s) first: {names}",
                "missing": missing,
            })

        STATE.run_date = d_day
        if not runner.trigger(STATE, d_day, step):
            return conflict({"error": "another run is in progress"})
        return json_response({"started": True, "step": step})


def _reset() -> Response:
    runner.cancel()
    cleared = STATE.reset_results()
    # The drawer promises "defaults restore on Reset" for the
    # per-(shift, role) bands, so clear those too.
    from ...allocator import caps as _caps
    _caps.reset_overrides()
    logger.info("[reset] cleared %s", cleared)
    return json_response({"reset": True, "cleared": cleared})


__all__ = ["RUN_STEPS", "parse_date", "register"]
