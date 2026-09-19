"""Drawer settings that write through past the session.

Shift limits are per-iteration and cleared by Reset. The airport code
and extraction-filter edits go into ``configs/config.yml``, so they
outlive the process and the engine sees them on the next run.
"""

from __future__ import annotations

from .. import overrides as override_ops
from ..core.responses import Response, bad_request, json_response, server_error
from ..core.router import Request, Router

#: Shift codes and roles a band override may name.
BAND_SHIFTS = ("M", "A", "N", "M1", "A1")
BAND_ROLES = ("STAFF", "ZC")


def register(router: Router) -> None:

    @router.post("/api/shift_limits")
    def shift_limits(req: Request) -> Response:
        """Drawer "Edit shift limits": a per-iteration band override for
        one (shift, role) bucket. Cleared by Reset."""
        body = req.json()
        shift = body.get("shift")
        role = body.get("role")
        try:
            min_v = int(body.get("min"))
            target_v = int(body.get("target"))
            max_v = int(body.get("max"))
        except (TypeError, ValueError):
            return bad_request("min, target, max must be integers")
        if shift not in BAND_SHIFTS:
            allowed = "/".join(BAND_SHIFTS)
            return bad_request(f"shift must be {allowed}, got {shift!r}")
        if role not in BAND_ROLES:
            allowed = " or ".join(BAND_ROLES)
            return bad_request(f"role must be {allowed}, got {role!r}")
        from ...allocator import caps as _caps
        from ...schemas import Role as _Role
        try:
            saved = _caps.set_iteration_band(
                shift, _Role.STAFF if role == "STAFF" else _Role.ZC,
                min_v=min_v, target_v=target_v, max_v=max_v,
            )
        except ValueError as exc:
            return bad_request(str(exc))
        return json_response({"saved": saved})

    @router.get("/api/extraction_filters")
    def list_filters(_req: Request) -> Response:
        try:
            filters = override_ops.list_extraction_filters()
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="list_extraction_filters failed")
        return json_response({"filters": filters})

    @router.post("/api/intl_airports")
    def add_airport(req: Request) -> Response:
        """Drawer "Add international airport code" — writes through to
        configs/config.yml so the engine sees it on the next run."""
        body = req.json()
        code = body.get("code")
        name = body.get("name")
        if not isinstance(code, str):
            return bad_request("body.code must be a string")
        try:
            payload = override_ops.add_intl_airport_code(
                code, name if isinstance(name, str) else None,
            )
        except ValueError as exc:
            return bad_request(str(exc))
        return json_response({"added": payload})

    @router.post("/api/extraction_filters/add")
    def add_filter(req: Request) -> Response:
        try:
            norm = override_ops.add_extraction_filter(req.json())
        except ValueError as exc:
            return bad_request(str(exc))
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="add_extraction_filter failed")
        return json_response({
            "filter": norm, "filters": override_ops.list_extraction_filters(),
        })

    @router.post("/api/extraction_filters/remove")
    def remove_filter(req: Request) -> Response:
        try:
            idx = int(req.json().get("index", 0))
        except (TypeError, ValueError):
            return bad_request("body.index must be an integer (1-based)")
        try:
            removed = override_ops.remove_extraction_filter(idx)
        except IndexError as exc:
            return bad_request(str(exc))
        return json_response({
            "removed": removed, "filters": override_ops.list_extraction_filters(),
        })


__all__ = ["BAND_ROLES", "BAND_SHIFTS", "register"]
