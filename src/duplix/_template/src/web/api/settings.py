"""Drawer settings that write through past the session.

Shift limits are per-iteration and cleared by Reset. The airport-code,
break-length and extraction-filter edits go into ``configs/config.yml``, and
the per-staff flight caps into ``configs/shift_limits.json``, so they
outlive the process and the engine sees them on the next run.
"""

from __future__ import annotations

from .. import overrides as override_ops
from .. import runner
from ..core.responses import (
    Response,
    bad_request,
    conflict,
    json_response,
    server_error,
)
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

    def _break_payload(minutes: int) -> dict[str, int]:
        from ...config import BREAK_MINUTES_HIGHEST, BREAK_MINUTES_LOWEST
        return {
            "length_minutes": minutes,
            "min": BREAK_MINUTES_LOWEST,
            "max": BREAK_MINUTES_HIGHEST,
        }

    @router.get("/api/break_length")
    def get_break_length(_req: Request) -> Response:
        """Setup sidebar "Shift break length" — the value the next
        Allocate run will use, plus the allowed range."""
        try:
            minutes = override_ops.get_break_length()
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="get_break_length failed")
        return json_response(_break_payload(minutes))

    @router.post("/api/break_length")
    def set_break_length(req: Request) -> Response:
        """Writes ``break_pass.length_minutes`` to configs/config.yml so
        the next Allocate run uses it."""
        try:
            minutes = override_ops.set_break_length(
                req.json().get("length_minutes"),
            )
        except ValueError as exc:
            return bad_request(str(exc))
        return json_response(_break_payload(minutes))

    def _caps_payload() -> dict[str, object]:
        from ...allocator import caps as _caps
        overrides = _caps.snapshot_iteration_overrides()["shift_role"]
        return {
            "caps": _caps.default_caps(),
            "overrides": {key: band["max"] for key, band in overrides.items()},
        }

    @router.get("/api/hard_caps")
    def get_hard_caps(_req: Request) -> Response:
        """Setup sidebar "Max flights per staff" table — the saved cap
        per shift and role, plus the cap of any per-run shift-limits
        override that shadows one until Reset."""
        return json_response(_caps_payload())

    @router.post("/api/hard_caps")
    def set_hard_caps(req: Request) -> Response:
        """Writes the caps to configs/shift_limits.json. Refused while a
        run is in flight, since the engine reads the caps all the way
        through one."""
        caps = req.json().get("caps")
        if not isinstance(caps, dict):
            return bad_request("body.caps must be an object of {shift: {role: cap}}")
        if runner.is_running():
            return conflict({"error": "a run is in progress — wait for it to finish"})
        from ...allocator import caps as _caps
        try:
            notes = _caps.set_default_caps(caps)
        except ValueError as exc:
            return bad_request(str(exc))
        return json_response({**_caps_payload(), "notes": notes})

    @router.get("/api/extraction_filters")
    def list_filters(_req: Request) -> Response:
        try:
            filters = override_ops.list_extraction_filters()
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="list_extraction_filters failed")
        return json_response({"filters": filters})

    @router.get("/api/intl_airports")
    def list_airports(_req: Request) -> Response:
        """Setup sidebar's international airport list, as configured in
        configs/config.yml."""
        try:
            airports = override_ops.list_intl_airports()
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="list_intl_airports failed")
        return json_response({"airports": airports})

    @router.post("/api/intl_airports")
    def add_airport(req: Request) -> Response:
        """Setup sidebar "Add international airport code" — writes through
        to configs/config.yml so the engine sees it on the next run."""
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
        return json_response({
            "added": payload, "airports": override_ops.list_intl_airports(),
        })

    @router.post("/api/intl_airports/remove")
    def remove_airport(req: Request) -> Response:
        """Setup sidebar's remove button on an airport chip — deletes the
        code from configs/config.yml; the next run treats flights from
        that airport as domestic."""
        code = req.json().get("code")
        if not isinstance(code, str):
            return bad_request("body.code must be a string")
        try:
            removed = override_ops.remove_intl_airport_code(code)
        except ValueError as exc:
            return bad_request(str(exc))
        return json_response({
            "removed": removed, "airports": override_ops.list_intl_airports(),
        })

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
