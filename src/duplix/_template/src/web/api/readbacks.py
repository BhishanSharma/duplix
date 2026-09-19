"""GET endpoints that only serialise state — no mutation, no side
effects.

The bulk of them are a straight ``path -> readback function`` mapping,
so they're registered from a table: a new tab needs a readback function
and one line here, not a new handler.
"""

from __future__ import annotations

from ...state import STATE
from .. import readback
from ..core.responses import Response, json_response
from ..core.router import Request, Router

#: Endpoints whose handler is exactly ``fn(STATE)``.
STATE_READBACKS = {
    "/api/inputs":             readback.read_inputs,
    "/api/server_date":        readback.read_server_date,
    "/api/dashboard":          readback.read_dashboard,
    "/api/allocations":        readback.read_allocations,
    "/api/workload":           readback.read_workload,
    "/api/pairs":              readback.read_pairs,
    "/api/warnings":           readback.read_warnings,
    "/api/unallocated":        readback.read_unallocated,
    "/api/recommendations":    readback.read_recommendations,
    "/api/overrides":          readback.read_overrides,
    "/api/plan":               readback.read_plan_summary,
    "/api/handlers":           readback.read_handlers,
    "/api/staffing":           readback.read_staffing,
    "/api/staff_names":        readback.read_staff_names,
    "/api/sv_portal/columns":  readback.read_sv_portal_columns,
}


def register(router: Router) -> None:
    for path, fn in STATE_READBACKS.items():
        router.add("GET", path, _state_handler(fn))

    @router.get("/api/shift_limits")
    def shift_limits(_req: Request) -> Response:
        return json_response(readback.read_shift_limits())

    @router.get("/api/override_types")
    def override_types(_req: Request) -> Response:
        """The canonical list of user-facing override types plus the
        per-type relevant columns, so the drawer always matches the
        backend enum and renders only the cells a row type cares about.
        Single source of truth lives in schemas."""
        from ...schemas import (
            OVERRIDE_TYPE_RELEVANT_COLS,
            OVERRIDE_TYPES_FOR_UI,
            OverrideType,
        )
        return json_response({
            "types": [t.value for t in OVERRIDE_TYPES_FOR_UI],
            "relevant_cols": {
                t.value: list(OVERRIDE_TYPE_RELEVANT_COLS[t])
                for t in OverrideType
            },
        })


def _state_handler(fn):
    def handler(_req: Request) -> Response:
        return json_response(fn(STATE))
    handler.__name__ = getattr(fn, "__name__", "readback")
    return handler


__all__ = ["STATE_READBACKS", "register"]
