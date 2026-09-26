"""Planning-stage readbacks: the Plan summary, nominated handlers, the
staffing recommendation, the roster name list and the shift limit bands.

These describe the state *before* a solve — what Plan produced and what
the solver is about to be asked to do.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ...schemas import STATUS_TO_P2F_HANDLER_SHIFT, OpsClass, Role
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

        # Workload-bearing flights only: GULF is never allocated, so it
        # must not inflate per-shift need.
        flights_per_shift: dict[str, int] = {s: 0 for s in SHIFTS}
        p2f_per_shift: dict[str, int] = {"M": 0, "A": 0, "N": 0}
        for ops, rows in state.cleaned.items():
            if ops is OpsClass.GULF:
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
    }


def read_handlers(state: AppState) -> dict[str, Any]:
    """The P2F handler picture for the dashboard — AND whether it's
    actually enough to allocate on.

    P2F handlers come from the roster first (a status like ``M/P2F`` or
    ``M/P2F/ZC``), with ``type=p2f`` override rows as the manual
    fallback / addition. An override nomination only counts if the
    employee is actually on that shift TODAY per the roster — a stale
    or mistyped shift on the override row (the same mismatch the
    solver's W212 check catches) is surfaced here as an ``issue``
    instead of silently looking fine, and does NOT count toward
    ``valid_count``.

    ``required_by_shift[shift]`` is ``ceil(p2f_flight_count / 8)`` — one
    handler can only be trusted with 8 P2F flights (W215) — computed
    from the cleaned schedule, so it's known right after Plan and
    before Allocate has to run. ``ready`` is True once every shift with
    P2F flights has at least that many *valid* nominations; the UI (and
    the run.js Allocate gate) uses this to decide whether Allocate
    should be clickable.
    """
    from ...allocator.windows import SHIFT_NOMINAL_MIN
    from ...schemas import OpsClass, P2F_HANDLER_ELIGIBLE_SHIFTS

    P2F_HANDLER_CAP = 8

    with state.lock:
        d_day = state.run_date

        shift_today_by_id: dict[str, str] = {}
        name_to_id: dict[str, str] = {}
        p2f_nominees: dict[str, list[dict[str, Any]]] = {}
        for av in state.availability:
            if av.name:
                name_to_id[av.name.strip().upper()] = av.employee_id
            if not is_live(state, av):
                continue
            shift_today_by_id[av.employee_id] = av.current_shift or ""
            status = canonical_status(av.status.value)
            shift = STATUS_TO_P2F_HANDLER_SHIFT.get(status) if status else None
            if shift:
                p2f_nominees.setdefault(shift, []).append({
                    "name": av.name, "employee_id": av.employee_id,
                    "shift": shift, "role": av.role.value,
                    "source": "roster",
                })

        issues: list[str] = []
        for row in state.overrides:
            rtype = (row.get("type") or "").lower()
            if rtype != "p2f":
                continue
            name = (row.get("employee") or "").strip()
            shift = (row.get("shift") or "").strip().upper()
            if not name or not shift:
                continue
            eid = name_to_id.get(name.upper())
            actual_shift = shift_today_by_id.get(eid) if eid else None
            if eid is None or actual_shift is None:
                issues.append(
                    f'P2F nomination "{name}" ({shift}): not an assignable '
                    "staff member on today's roster."
                )
                continue
            if actual_shift != shift:
                issues.append(
                    f'P2F nomination "{name}" is listed for shift {shift}, '
                    f"but today's roster has them on {actual_shift or 'OFF'}. "
                    "Fix the override's shift or nominate someone else."
                )
                continue
            already = any(
                n["employee_id"] == eid for n in p2f_nominees.get(shift, [])
            )
            if not already:
                p2f_nominees.setdefault(shift, []).append({
                    "name": name, "employee_id": eid, "shift": shift,
                    "role": "", "source": "override",
                })

        # How many P2F flights actually fall in each shift's window —
        # known from Plan (state.cleaned), no Allocate needed.
        p2f_count_by_shift: dict[str, int] = {
            s: 0 for s in P2F_HANDLER_ELIGIBLE_SHIFTS
        }
        for row in state.cleaned.get(OpsClass.P2F, []):
            std_min = row.std.hour * 60 + row.std.minute
            if row.date and d_day and row.date == d_day + timedelta(days=1):
                std_min += 24 * 60
            for shift, (start, end) in SHIFT_NOMINAL_MIN.items():
                if shift not in P2F_HANDLER_ELIGIBLE_SHIFTS:
                    continue
                if start <= std_min <= end:
                    p2f_count_by_shift[shift] += 1
                    break

        run_date = run_date_iso(state)

    required_by_shift: dict[str, int] = {}
    valid_count_by_shift: dict[str, int] = {}
    shift_status: dict[str, str] = {}
    for shift in P2F_HANDLER_ELIGIBLE_SHIFTS:
        count = p2f_count_by_shift.get(shift, 0)
        needed = -(-count // P2F_HANDLER_CAP) if count else 0  # ceil
        have = len(p2f_nominees.get(shift, []))
        required_by_shift[shift] = needed
        valid_count_by_shift[shift] = have
        if needed == 0:
            shift_status[shift] = "not_needed"
        elif have >= needed:
            shift_status[shift] = "ok"
        else:
            shift_status[shift] = "missing"

    plan_has_run = bool(d_day) and bool(state.cleaned)
    ready = (not plan_has_run) or all(
        shift_status[s] != "missing" for s in P2F_HANDLER_ELIGIBLE_SHIFTS
    )

    all_nominees = [n for shift in p2f_nominees for n in p2f_nominees[shift]]

    return {
        "run_date": run_date,
        "p2f": all_nominees,
        "missing_p2f_shifts": sorted(
            s for s, st in shift_status.items() if st == "missing"
        ),
        "required_by_shift": required_by_shift,
        "valid_count_by_shift": valid_count_by_shift,
        "p2f_flight_count_by_shift": p2f_count_by_shift,
        "shift_status": shift_status,
        "issues": issues,
        "plan_has_run": plan_has_run,
        "ready": ready,
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


def read_roster_full(state: AppState) -> dict[str, Any]:
    """Every staff/AM/ZC row for the run date, INCLUDING anyone currently
    off / on leave / sick (``read_staff_names`` drops those, since it
    drives dropdowns that only make sense for someone on a shift today).

    Drives the Roster Change pane's search: an assigner who marked
    someone off needs to be able to find that person again to put them
    back on a shift, so the search list can't shrink when a person is
    taken off it.
    """
    names: list[dict[str, Any]] = []
    seen: set[str] = set()
    with state.lock:
        d_iso = run_date_iso(state)
        for av in state.availability:
            if d_iso and av.date.isoformat() != d_iso:
                continue
            name = av.name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append({
                "name": name,
                "role": av.role.value,
                "shift": av.current_shift or "",
                "status": av.status.value,
                "assignable": av.assignable,
            })
    names.sort(key=lambda x: x["name"])
    return {"run_date": d_iso, "names": names}


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


