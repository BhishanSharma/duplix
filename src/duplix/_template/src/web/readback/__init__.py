"""Serialise the in-memory state into the JSON shapes the UI needs.

Every function takes the ``AppState`` and returns plain dicts/lists
ready for ``json.dumps``. Nothing here reads a file, so a GET is cheap
and can re-serialise on every request.

The package is split by what the caller is looking at:

    common      run date / INTL codes / roster-liveness predicates
    dashboard   headline stat cards + the at-risk table
    tables      the per-tab row lists (allocations, workload, pairs, ...)
    planning    Plan summary, handlers, staffing, roster, shift bands
    session     uploads, override rows, SV-portal columns

Import the names from here, not from the sub-modules — this is the
stable surface the API layer binds its routes to.
"""

from __future__ import annotations

from ...schemas import Severity
from .dashboard import read_dashboard
from .planning import (
    read_handlers,
    read_plan_summary,
    read_roster_full,
    read_shift_limits,
    read_staff_names,
    read_staffing,
)
from .session import (
    read_inputs,
    read_overrides,
    read_roster_counts,
    read_server_date,
    read_sv_portal_columns,
)
from .tables import (
    read_allocations,
    read_pairs,
    read_recommendations,
    read_unallocated,
    read_warnings,
    read_workload,
)

__all__ = [
    "Severity",
    "read_allocations",
    "read_dashboard",
    "read_handlers",
    "read_inputs",
    "read_overrides",
    "read_pairs",
    "read_plan_summary",
    "read_recommendations",
    "read_roster_counts",
    "read_roster_full",
    "read_server_date",
    "read_shift_limits",
    "read_staff_names",
    "read_staffing",
    "read_sv_portal_columns",
    "read_unallocated",
    "read_warnings",
    "read_workload",
]
