"""Dashboard headline readback — the stat cards and the at-risk table.

One function, deliberately: the dashboard is the aggregate view, so it
touches allocations, roster and warnings at once. Per-tab detail lives
in the sibling modules.
"""

from __future__ import annotations

from typing import Any

from ...state import AppState
from .common import run_date_iso



def read_dashboard(state: AppState) -> dict[str, Any]:
    """Run-status block + the headline counts behind the stat cards."""
    with state.lock:
        # Per-class cards show CLEANED counts (flights to plan), so the
        # numbers are real right after Plan rather than zeros that only
        # fill in after Allocate.
        by_class = {ops.value: len(rows) for ops, rows in state.cleaned.items()}
        special_sub = {
            "test": by_class.get("test", 0),
            "ferry": by_class.get("ferry", 0),
            "charter": by_class.get("charter", 0),
        }

        # "Flights allocated" counts allocation rows. A pre-plan-deferred
        # row has empty staff but a populated planned_by — someone is
        # preparing it, so it is not unallocated.
        flights_total = len(state.allocations)

        staff_at_risk: list[dict[str, Any]] = []
        for w in state.workload:
            # At risk = real cap violations only. Under-preferred is just
            # a light load; above-preferred is informational; handlers
            # exceed cap legitimately because their P2F/NORSE flights are
            # exempt from H16.
            joined = "; ".join(w.violations)
            lowered = joined.lower()
            if not any(p in lowered for p in (
                "above hard cap", "exceeds cap", "exceeds hard cap",
                "over hard cap",
            )):
                continue
            staff_at_risk.append({
                "employee_id": w.employee_id,
                "name": w.name,
                "shift": w.shift or "",
                "actual": w.actual,
                "target_preferred": w.target_preferred,
                "hard_cap": w.hard_cap,
                "violations": joined,
            })

        warnings_total = len(state.warnings)

        return {
            "run_date": run_date_iso(state),
            "last_run": state.last_run,
            "mode": state.mode,
            "solver_status": state.solver_status,
            "duration_s": state.duration_s,
            "has_inputs": not state.missing_inputs(),
            "missing_inputs": state.missing_inputs(),
            "stats": {
                "flights_total": flights_total,
                "flights_unallocated": len(state.unallocated),
                "day_ops": by_class.get("day", 0),
                "night_ops": by_class.get("night", 0),
                "p2f": by_class.get("p2f", 0),
                "norse": by_class.get("norse", 0),
                "special_ops": sum(special_sub.values()),
                "special_ops_test": special_sub["test"],
                "special_ops_ferry": special_sub["ferry"],
                "special_ops_charter": special_sub["charter"],
                "staff_total": len(state.workload),
                "staff_at_risk": len(staff_at_risk),
                "warnings_total": warnings_total,
            },
            "staff_at_risk": staff_at_risk[:10],
        }


