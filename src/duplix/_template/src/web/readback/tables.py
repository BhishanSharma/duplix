"""Per-tab table readbacks: allocations, workload, pairs, warnings,
unallocated and the recommender suggestions that hang off it.

Each returns ``{"rows": [...]}`` (plus a count where the UI shows one),
so a new tab is a new function here and a new route in ``api``.
"""

from __future__ import annotations

from typing import Any

from ...schemas import OpsClass
from ...state import AppState
from .common import intl_codes, run_date_iso



def read_allocations(state: AppState) -> dict[str, Any]:
    """Every allocation row as one flat list, with the ops_class and
    the international flags the UI colours by.

    P2F precedence: a P2F flight whose DEP happens to be in the INTL
    list (DAC, KMG…) is still a P2F flight — it never gets the INTL
    tint, which has its own meaning in the P2F lane.
    """
    intl_set = intl_codes()
    with state.lock:
        # (flt, dep, arr, std) -> ops_class, from the cleaned flights.
        ops_by_key: dict[tuple[str, str, str, str], str] = {}
        for ops, rows in state.cleaned.items():
            for r in rows:
                ops_by_key[(
                    r.flt, r.dep.upper(), r.arr.upper(),
                    r.std.isoformat(timespec="minutes"),
                )] = ops.value

        rows_out: list[dict[str, Any]] = []
        for r in state.allocations:
            dep = r.dep.upper()
            arr = r.arr.upper()
            std = r.std.isoformat(timespec="minutes")
            ops_class = ops_by_key.get((r.flt, dep, arr, std), "")
            is_p2f = ops_class == "p2f"
            key = f"{r.flt}|{dep}|{arr}|{std}|{r.date.isoformat()}"
            prev_staff = state.prev_staff_by_key.get(key, "")
            rows_out.append({
                "sheet": r.sheet_target.value,
                "date": r.date.isoformat(),
                "flt": r.flt,
                "dep": r.dep,
                "arr": r.arr,
                "std": std,
                "pax": r.pax,
                "staff": r.staff_name,
                "planned_by": r.planned_by_name or "",
                "relieved_by": r.relieved_by_name or "",
                "warning": r.warning or "",
                "is_international": dep in intl_set and not is_p2f,
                "is_arr_international": arr in intl_set and not is_p2f,
                "ops_class": ops_class,
                # Only "redistributed" when both sides had a staff and
                # they differ — a newly-filled slot is not a move.
                "redistributed_from": (
                    prev_staff
                    if prev_staff and r.staff_name and prev_staff != r.staff_name
                    else ""
                ),
            })

        # Gulf flights are extracted, never allocated. They show up
        # under sheet="Removed" so the Allocations filter can reach them.
        for r in state.cleaned.get(OpsClass.GULF, []):
            rows_out.append({
                "sheet": "Removed",
                "date": r.date.isoformat(),
                "flt": r.flt,
                "dep": r.dep,
                "arr": r.arr,
                "std": r.std.isoformat(timespec="minutes"),
                "pax": r.load,
                "staff": "",
                "planned_by": "",
                "relieved_by": "",
                "warning": "extracted — not allocated",
                "is_international": False,
                "is_arr_international": False,
                "ops_class": "gulf",
                "redistributed_from": "",
            })
    return {"rows": rows_out}


def read_workload(state: AppState) -> dict[str, Any]:
    with state.lock:
        return {"rows": [
            {
                "employee_id": w.employee_id,
                "name": w.name,
                "shift": w.shift or "",
                "role": w.role.value,
                "target_preferred": w.target_preferred,
                "target_acceptable_max": w.target_acceptable_max,
                "hard_cap": w.hard_cap,
                "actual": w.actual,
                "deviation_below_preferred": w.deviation_below_preferred,
                "deviation_above_preferred": w.deviation_above_preferred,
                "violations": "; ".join(w.violations),
            }
            for w in state.workload
        ]}


def read_pairs(state: AppState) -> dict[str, Any]:
    with state.lock:
        return {"rows": [
            {
                "boundary": p.boundary.value,
                "pair_role": p.pair_role.value,
                "preplan_count": p.preplan_count,
                "prev_employee_id": p.prev_employee_id or "",
                "prev_name": p.prev_name or "",
                "prev_shift": p.prev_shift or "",
                "next_employee_id": p.next_employee_id or "",
                "next_name": p.next_name or "",
                "next_shift": p.next_shift or "",
            }
            for p in state.pairs
        ]}


def read_warnings(state: AppState) -> dict[str, Any]:
    with state.lock:
        return {"rows": [
            {
                "severity": w.severity.value,
                "code": w.code,
                "name": w.name or "",
                "date": w.date.isoformat() if w.date else "",
                "message": w.message,
            }
            for w in state.warnings
        ]}


def read_unallocated(state: AppState) -> dict[str, Any]:
    """Every unallocated flight. ``count`` drives the prominent card;
    an empty list means the UI shows the green all-allocated message."""
    with state.lock:
        rows = [dict(r) for r in state.unallocated]
    return {"count": len(rows), "rows": rows}


def read_recommendations(state: AppState) -> dict[str, Any]:
    """Phase-R suggestions per unallocated flight. Stable empty shape
    when the solver hasn't run, so the panel renders without erroring."""
    with state.lock:
        data = dict(state.recommendations)
    data.setdefault("recommendations", [])
    data.setdefault("n_unallocated", len(data["recommendations"]))
    data.setdefault("ops_day", run_date_iso(state) or None)
    return data


