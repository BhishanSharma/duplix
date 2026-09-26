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

        # Belt-and-suspenders: the dashboard is expected to grey out
        # Allocate until P2F handlers are sorted (see plan.js /
        # handlers.js), but a direct API call — or a stale tab — could
        # still reach here. Only blocks when Plan has already run for
        # this date (same date requested) with a real gap; never blocks
        # the "all" step's own Plan phase when nothing's been planned
        # yet, since that will surface the same gap in the Warnings tab
        # after the solve.
        if step in ("all", "step3") and d_day == STATE.run_date:
            from .. import readback
            handlers_state = readback.read_handlers(STATE)
            if handlers_state.get("plan_has_run") and not handlers_state.get("ready"):
                missing_shifts = handlers_state.get("missing_p2f_shifts", [])
                return conflict({
                    "error": (
                        "P2F handler nomination missing or invalid for "
                        f"shift(s): {', '.join(missing_shifts)}. Fix it in "
                        "the Override drawer, then Plan again before "
                        "Allocate."
                    ),
                    "missing_p2f_shifts": missing_shifts,
                    "issues": handlers_state.get("issues", []),
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
