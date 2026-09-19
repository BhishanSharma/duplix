"""The Override drawer's write surface: the row table, the staged
add/remove forms, and the apply pass that materialises staged rows onto
the data sheets.

Every mutation here is session-scoped — rows live in ``STATE`` until
the server restarts. Reset keeps them on purpose, so an assigner can
re-run without retyping.
"""

from __future__ import annotations

import logging

from ...state import STATE
from .. import overrides as override_ops
from ..core.responses import (
    Response,
    bad_request,
    json_response,
    server_error,
)
from ..core.router import Request, Router
from .runs import parse_date

logger = logging.getLogger("src.web")


def _row_index(req: Request) -> int | None:
    """The ``{index}`` path param as an int, or None if it isn't one."""
    try:
        return int(req.params["index"])
    except (KeyError, TypeError, ValueError):
        return None


def register(router: Router) -> None:

    @router.post("/api/overrides")
    def add_row(req: Request) -> Response:
        values = req.json().get("values")
        if not isinstance(values, list):
            return bad_request("body.values must be a list of strings")
        index = override_ops.add_override(STATE, [str(v) for v in values])
        return json_response({"index": index})

    @router.put("/api/overrides/{index}")
    def update_row(req: Request) -> Response:
        row_index = _row_index(req)
        if row_index is None:
            return bad_request(f"row index must be an integer, got {req.params['index']!r}")
        body = req.json()
        values = body.get("values")
        if not isinstance(values, list):
            return bad_request("body.values must be a list of strings")
        try:
            override_ops.update_override(
                STATE, row_index, [str(v) for v in values],
            )
        except IndexError as exc:
            return bad_request(str(exc))

        # Re-apply staged rows after an edit so add_flight / remove_flight
        # / add_staff / remove_staff / change_role / sick take effect on
        # the data immediately; the dashboard refresh then shows it.
        applied: dict[str, int] = {}
        d_day = parse_date(body.get("date"))
        if d_day is not None:
            from ... import staged_overrides as _so
            applied = _so.apply_staged_overrides(STATE, d_day)
        return json_response({"updated": True, "applied": applied})

    @router.delete("/api/overrides/{index}")
    def delete_row(req: Request) -> Response:
        row_index = _row_index(req)
        if row_index is None:
            return bad_request(f"row index must be an integer, got {req.params['index']!r}")
        try:
            override_ops.delete_override(STATE, row_index)
        except IndexError as exc:
            return bad_request(str(exc))
        return json_response({"deleted": True})

    @router.post("/api/overrides/apply_staged")
    def apply_staged(req: Request) -> Response:
        d_day = parse_date(req.json().get("date"))
        if d_day is None:
            return bad_request("body.date required (YYYY-MM-DD)")
        from ... import staged_overrides as _so
        try:
            counts = _so.apply_staged_overrides(STATE, d_day)
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="apply_staged failed")
        return json_response({"applied": counts})

    @router.post("/api/staged_form/{op}")
    def staged_form(req: Request) -> Response:
        """Drawer forms: add_flight / remove_flight / add_staff /
        remove_staff. Writes the override row AND applies it immediately
        so the dashboard reflects the projected state on next refresh.

        ``ops_date`` is the day being planned and must be ISO. It is
        kept separate from the row's own ``date`` column, which a
        remove_flight row carries as MM/DD — overloading one key for
        both meant the MM/DD value was parsed as the ops date and every
        remove_flight submission was rejected. ``date`` is still read as
        a fallback so an older client keeps working.
        """
        from ...schemas import STAGED_FORM_OVERRIDE_TYPES
        op_type = req.params["op"]
        valid = {t.value for t in STAGED_FORM_OVERRIDE_TYPES}
        if op_type not in valid:
            return bad_request(
                f"unknown staged form op: {op_type!r}. valid: {sorted(valid)}"
            )
        body = req.json()
        d_day = parse_date(body.get("ops_date")) or parse_date(body.get("date"))
        if d_day is None:
            return bad_request("body.ops_date required (YYYY-MM-DD)")
        payload = {
            k: str(v).strip() for k, v in body.items()
            if k != "ops_date" and isinstance(v, (str, int, float)) and str(v).strip()
        }
        try:
            index = override_ops.write_staged_override(STATE, op_type, payload)
            from ... import staged_overrides as _so
            applied = _so.apply_staged_overrides(STATE, d_day)
        except Exception as exc:  # noqa: BLE001
            logger.exception("staged form %s failed", op_type)
            return server_error(exc, context=f"{op_type} failed")
        logger.info(
            "[staged-form] %s: row %s written, applied=%s", op_type, index, applied,
        )
        return json_response({
            "ok": True, "index": index, "op": op_type, "applied": applied,
        })


__all__ = ["register"]
