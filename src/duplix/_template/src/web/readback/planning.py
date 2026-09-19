"""Planning-stage readbacks: the Plan summary, nominated handlers, the
staffing recommendation, the roster name list and the shift limit bands.

These describe the state *before* a solve — what Plan produced and what
the solver is about to be asked to do.
"""

from __future__ import annotations

from typing import Any

from ...schemas import STATUS_TO_P2F_HANDLER_SHIFT, AllocationSheet, OpsClass, Role
from ...state import AppState
from .common import SHIFTS, canonical_status, is_live, run_date_iso



def read_plan_summary(state: AppState) -> dict[str, Any]:
    """Structured plan payload so the UI can draw one coverage bar per
    shift plus the handler bars.

    ``needed`` is derived live as
    ``ceil(flights_in_that_shift / preferred_flights_per_staff)`` using
    the current (possibly drawer-overridden) preferred targets. Flights
    are bucketed into shift windows by STD with the same windowing the
    engine uses, so the bar reflects the constraints the solver will
    actually enforce.
    """
    from ...allocator.caps import preferred_target_for_shift_role
    from ...allocator.windows import SHIFT_NOMINAL_MIN

    def _shift_for(std_min: int) -> str:
        for shift, (start, end) in SHIFT_NOMINAL_MIN.items():
            if start <= std_min <= end:
                return shift
        return ""

    def _ceil_div(a: int, b: int) -> int:
        return -(-a // b) if b else 0

    with state.lock:
        flights_by_class = {o.value: 0 for o in OpsClass}
        for ops, rows in state.cleaned.items():
            flights_by_class[ops.value] = len(rows)

        # Workload-bearing flights only: NORSE has its own handler and
        # GULF is never allocated, so neither inflates per-shift need.
        flights_per_shift: dict[str, int] = {s: 0 for s in SHIFTS}
        p2f_per_shift: dict[str, int] = {"M": 0, "A": 0, "N": 0}
        for ops, rows in state.cleaned.items():
            if ops in (OpsClass.NORSE, OpsClass.GULF):
                continue
            for r in rows:
                std_min = r.std.hour * 60 + r.std.minute
                sh = _shift_for(std_min)
                if sh:
                    flights_per_shift[sh] += 1
                if ops is OpsClass.P2F and sh in p2f_per_shift:
                    p2f_per_shift[sh] += 1

        staff_by_shift: dict[str, int] = {s: 0 for s in SHIFTS}
        nominated_p2f: dict[str, bool] = {s: False for s in SHIFTS}
        for av in state.availability:
            if not is_live(state, av):
                continue
            if av.current_shift in staff_by_shift:
                staff_by_shift[av.current_shift] += 1
            status = canonical_status(av.status.value)
            handler_shift = (
                STATUS_TO_P2F_HANDLER_SHIFT.get(status) if status else None
            )
            if handler_shift:
                nominated_p2f[handler_shift] = True

        norse_nominee_count = sum(
            1 for row in state.overrides
            if row.get("type", "").lower() == "norse" and row.get("employee")
        )
        plan_text = state.plan_text
        run_date = run_date_iso(state)

    coverage_by_shift: dict[str, dict[str, Any]] = {}
    for shift in SHIFTS:
        flights_in_shift = flights_per_shift[shift]
        # STAFF preferred, not ZC — ZCs are a small slice of the pool and
        # the bar flags gross under-staffing, not role mix.
        pref = preferred_target_for_shift_role(shift, Role.STAFF)
        coverage_by_shift[shift] = {
            "present": staff_by_shift[shift],
            "needed": _ceil_div(flights_in_shift, pref) if pref else 0,
            "flights_in_shift": flights_in_shift,
            "preferred_per_staff": pref,
        }

    p2f_bars = {
        shift: {
            "flights": p2f_per_shift[shift],
            "needed": _ceil_div(p2f_per_shift[shift], 8),
            "nominated": 1 if nominated_p2f.get(shift) else 0,
        }
        for shift in ("M", "A", "N")
    }
    norse_flights = flights_by_class["norse"]
    day_staff = sum(staff_by_shift[s] for s in ("M", "A", "M1", "A1"))

    return {
        "text": plan_text,
        "run_date": run_date,
        "flights": flights_by_class,
        "total_flights": sum(flights_by_class.values()),
        "staff_by_shift": staff_by_shift,
        "day_staff_total": day_staff,
        "night_staff_total": staff_by_shift["N"],
        "coverage_by_shift": coverage_by_shift,
        "p2f_bars": p2f_bars,
        "norse": {
            "flights": norse_flights,
            "needed": _ceil_div(norse_flights, 10),
            "nominated": norse_nominee_count,
        },
    }


def read_handlers(state: AppState) -> dict[str, Any]:
    """The P2F + NORSE handler picture for the dashboard.

    P2F handlers come from the roster first (a status like ``M/P2F`` or
    ``M/P2F/ZC``), with a ``type=p2f`` override row as the manual
    fallback. NORSE has no roster shorthand — it comes from a
    ``type=norse`` override, or, failing that, from whoever the engine
    auto-picked on the NORSE allocations.
    """
    p2f_by_shift: dict[str, dict[str, str]] = {}
    norse: list[dict[str, str]] = []

    with state.lock:
        for av in state.availability:
            if not is_live(state, av):
                continue
            status = canonical_status(av.status.value)
            shift = STATUS_TO_P2F_HANDLER_SHIFT.get(status) if status else None
            if shift and shift not in p2f_by_shift:
                p2f_by_shift[shift] = {
                    "name": av.name, "shift": shift,
                    "role": av.role.value, "source": "roster",
                }

        for row in state.overrides:
            rtype = row.get("type", "").lower()
            name = row.get("employee", "").strip()
            if not name:
                continue
            if rtype == "p2f":
                shift = row.get("shift", "").strip().upper()
                if shift and shift not in p2f_by_shift:
                    p2f_by_shift[shift] = {
                        "name": name, "shift": shift,
                        "role": "", "source": "override",
                    }
            elif rtype == "norse":
                norse.append({"name": name, "source": "override"})

        # No explicit nomination? Fall back to whoever actually flew the
        # NORSE flights — the engine auto-picks a non-N ZC and emits a
        # W213 INFO, and showing "not nominated" made assigners think
        # their nomination had been ignored.
        if not norse:
            seen: set[str] = set()
            for r in state.allocations:
                if r.sheet_target is not AllocationSheet.NORSE:
                    continue
                if r.staff_name and r.staff_name not in seen:
                    seen.add(r.staff_name)
                    norse.append({"name": r.staff_name, "source": "auto-pick"})

        run_date = run_date_iso(state)

    return {
        "run_date": run_date,
        "p2f": list(p2f_by_shift.values()),
        "norse": norse,
        "missing_p2f_shifts": sorted({"M", "A", "N"} - set(p2f_by_shift)),
        # "nominated" means an explicit override row. Auto-pick fills the
        # operational gap but is flagged as such, so the assigner knows
        # to nominate explicitly if they want someone else.
        "norse_nominated": any(n["source"] == "override" for n in norse),
        "norse_auto_picked": any(n["source"] == "auto-pick" for n in norse),
    }


def read_staff_names(state: AppState) -> dict[str, Any]:
    """Every assignable staff + AM/ZC name for the run date, with the
    shift each is rostered on. Drives the drawer's employee dropdown and
    the dashboard's roster-by-shift block, so ``remove_staff`` / ``sick``
    take effect here without waiting for a re-Plan."""
    names: list[dict[str, str]] = []
    seen: set[str] = set()
    with state.lock:
        for av in state.availability:
            if not is_live(state, av):
                continue
            name = av.name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            status = av.status.value
            names.append({
                "name": name,
                "role": av.role.value,
                "shift": av.current_shift or "",
                # ZCs arrive via the AM roster with role='AM' but a
                # status like 'M/ZC' — the UI needs that to tag them.
                "status": status,
                "is_zc": "ZC" in status.upper(),
            })
        run_date = run_date_iso(state)
    names.sort(key=lambda x: x["name"])
    return {"run_date": run_date, "names": names}


def read_staffing(state: AppState) -> dict[str, Any]:
    with state.lock:
        return {"run_date": run_date_iso(state), "rows": list(state.staffing)}


def read_shift_limits() -> dict[str, Any]:
    """Current (shift, role) bands so the drawer's "Edit shift limits"
    form can pre-fill. Merges the JSON defaults with any iteration
    override — exactly what the engine will see on its next run."""
    from ...allocator.caps import get_band, snapshot_iteration_overrides

    bands: dict[str, dict[str, Any]] = {}
    for shift in SHIFTS:
        bands[shift] = {}
        for role_name, role in (("STAFF", Role.STAFF), ("ZC", Role.ZC)):
            bands[shift][role_name] = get_band(shift, role) or {}
    return {
        "bands": bands,
        "iteration_overrides": snapshot_iteration_overrides(),
    }


