"""Zone Controller list: read, add, remove — one list per date.

The list lives in ``zc_store`` (saved on disk, carried over from the
previous day until edited). Every change is applied to the working data
straight away, so the dashboard reflects it before Allocate is clicked;
the next Plan re-applies it from the store.
"""

from __future__ import annotations

import logging
from datetime import date as date_t

from ... import zc_store
from ...state import STATE
from .. import runner
from ..core.responses import Response, bad_request, conflict, json_response
from ..core.router import Request, Router
from .runs import parse_date

logger = logging.getLogger("src.web")


def _payload(d_day: date_t, **extra: object) -> dict[str, object]:
    names, source, carried_from = zc_store.effective(d_day)
    return {
        "date": d_day.isoformat(),
        "names": names,
        # saved   = this date has its own list
        # carried = inherited from an earlier date until edited
        # none    = nothing saved on or before this date
        "source": source,
        "carried_from": carried_from.isoformat() if carried_from else None,
        **extra,
    }


def _rebuild(d_day: date_t) -> dict[str, object]:
    """Re-derive today's availability from the rosters and re-apply the
    staged rows plus the ZC list.

    A rebuild (rather than patching roles in place) is what makes
    *removing* a ZC work: the roster row is the only place that still
    knows whether that person is STAFF or AM. Only done when the date
    being edited is the one that has been planned; otherwise the list is
    simply saved and applied by the next Plan for that date.
    """
    with STATE.lock:
        planned = bool(STATE.availability) and STATE.run_date == d_day
    if not planned:
        return {"applied": False, "reason": "not planned yet"}

    from ...step2_extract_roster import run as run_step2

    # step2 replaces state.warnings; the ones on screen belong to the
    # last Plan / Allocate, so put them back afterwards.
    with STATE.lock:
        saved_warnings = list(STATE.warnings)
    try:
        counts = run_step2(STATE, d_day, runner.CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 — list is saved; say why it didn't apply
        logger.warning("[zc] roster re-read failed: %s", exc)
        return {"applied": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        with STATE.lock:
            STATE.warnings = saved_warnings
    if counts.get("ABORTED"):
        return {"applied": False, "reason": "roster check failed — see warnings"}
    return {"applied": True}


def _date_or_error(raw: str | None) -> tuple[date_t | None, Response | None]:
    d_day = parse_date(raw)
    if d_day is None:
        return None, bad_request("date required (YYYY-MM-DD)")
    return d_day, None


def register(router: Router) -> None:

    @router.get("/api/zc")
    def get_zc(req: Request) -> Response:
        d_day, err = _date_or_error((req.query.get("date") or [""])[0])
        if err:
            return err
        return json_response(_payload(d_day))

    @router.post("/api/zc/add")
    def add_zc(req: Request) -> Response:
        body = req.json()
        d_day, err = _date_or_error(body.get("date"))
        if err:
            return err
        name = str(body.get("name") or "").strip()
        if not name:
            return bad_request("name required")
        if runner.is_running():
            return conflict({"error": "a run is in progress — wait for it to finish"})
        zc_store.add(d_day, name)
        return json_response(_payload(d_day, **_rebuild(d_day)))

    @router.post("/api/zc/remove")
    def remove_zc(req: Request) -> Response:
        body = req.json()
        d_day, err = _date_or_error(body.get("date"))
        if err:
            return err
        name = str(body.get("name") or "").strip()
        if not name:
            return bad_request("name required")
        if runner.is_running():
            return conflict({"error": "a run is in progress — wait for it to finish"})
        zc_store.remove(d_day, name)
        return json_response(_payload(d_day, **_rebuild(d_day)))


__all__ = ["register"]
