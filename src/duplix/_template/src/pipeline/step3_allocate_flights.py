"""Allocate stage (step 3) — the flight allocation orchestrator.

Reads from state:
  cleaned flights (every ops class except GULF)  — what there is to allocate
  availability                                   — who is on shift for D
  overrides                                      — per-day operator intent
  the uploaded AM roster                         — AM/ZC rows (ZC shift recovery)

Builds:
  StaffMember per assignable staff (STAFF + ZC; AM excluded)
  EligibilityContext (P2F handler + flight times)
  Sparse eligibility matrix
  Pair list (deterministic from row order; secondary handover-only when
    A surplus exists; split-1+2 at A→N; N→M cross-role for excess M-ZCs)

Solves with CP-SAT (hard constraints H1, H10, H16, H17;
S1+S3+S4+S5+S6+S7 enabled).

Writes back to state:
  allocations (each row tagged with its DayOps / NightOps / P2F lane)
  pairs, workload summary, unallocated list
  warnings (W201/W210/W211 surfaced; appended to the Plan stage's)
  recommendations for whatever could not be placed
  the run-status stamp
"""

from __future__ import annotations

import time as _time
from collections import defaultdict
from datetime import date as date_t
from pathlib import Path

from ..allocator.caps import aggregate_capacity
from ..allocator.eligibility import (
    EligibilityContext,
    build_matrix,
    diagnose_unassignable,
    tag_awkward_window,
)
from ..allocator.pairings import PairGenerationError, generate_pairs
from ..allocator.postsolve import (
    assemble_allocation_rows,
    build_workload_summary,
)
from ..allocator.windows import std_to_ops_day_minutes
from ..config import Config, load_config
from ..io.readers import (
    read_p2f_exclusions,
    read_p2f_nominations,
    read_per_staff_overrides,
    read_raise_cap_overrides,
    read_role_change_overrides,
    read_sick_overrides,
    read_skip_intl_removal_overrides,
    read_skip_p2f_buffer_overrides,
    read_waive_h10_pairs,
)
from ..io.roster_library import read_library
from ..state import AppState
from ..schemas import (
    AllocationResult,
    AMRosterRow,
    AvailabilityRow,
    CleanFlightRow,
    CrewStatus,
    FlightInput,
    OpsClass,
    Role,
    Severity,
    ShiftCode,
    StaffMember,
    WarningRow,
)
from ..solver.allocator_cpsat import AllocationSolverResult, solve_allocation

# ---------- ZC shift recovery helpers ----------
# 2026-05-27 (centralization): these three maps are DERIVED from the
# central taxonomy in schemas.py — one source of truth. Step3's
# branching logic depends on them being three DISJOINT subsets of
# CrewStatus, so they stay as separate local lookups:
#   _ZC_STATUS_TO_SHIFT          — ZC-only variants (M/ZC, A/ZC, …)
#   _P2F_HANDLER_STATUS_TO_SHIFT — plain P2F handler variants (no ZC)
#   _ZC_P2F_HANDLER_STATUS_TO_SHIFT — combined ZC + P2F variants
# Adding a new ZC variant to STATUS_IS_ZC (in schemas.py) auto-flows
# here. Adding a new P2F-handler status to STATUS_TO_P2F_HANDLER_SHIFT
# auto-flows here. No more "I forgot to update this map" bugs.
from ..schemas import (
    STATUS_IS_ZC as _CENTRAL_ZC,
    STATUS_TO_P2F_HANDLER_SHIFT as _CENTRAL_P2F_SHIFT,
    STATUS_TO_SHIFT as _CENTRAL_SHIFT,
)

_ZC_STATUS_TO_SHIFT: dict[CrewStatus, ShiftCode] = {
    s: _CENTRAL_SHIFT[s]                                # type: ignore[misc]
    for s in _CENTRAL_ZC
    if s not in _CENTRAL_P2F_SHIFT and _CENTRAL_SHIFT.get(s) is not None
}

_P2F_HANDLER_STATUS_TO_SHIFT: dict[CrewStatus, ShiftCode] = {
    s: sh for s, sh in _CENTRAL_P2F_SHIFT.items()
    if s not in _CENTRAL_ZC
}

_ZC_P2F_HANDLER_STATUS_TO_SHIFT: dict[CrewStatus, ShiftCode] = {
    s: sh for s, sh in _CENTRAL_P2F_SHIFT.items()
    if s in _CENTRAL_ZC
}


def _flight_key(flt: str, dep: str, arr: str, std_iso: str, date_iso: str) -> str:
    """The key ``FlightInput.unique_id`` uses, built from loose parts."""
    return f"{flt}|{dep}|{arr}|{std_iso}|{date_iso}"


def _prior_allocations_as_hints(
    state: AppState,
    name_to_id: dict[str, str],
) -> dict[str, str]:
    """Build ``{unique_id: employee_id}`` from the PREVIOUS solve's rows,
    which are still on the state until this run overwrites them.

    CP-SAT consumes this as a warm start, which drops re-solve wall
    clock 30-60 %. Without it every Allocate runs cold even when the
    prior assignments are 99 % still feasible — which is why a sick /
    recommender re-solve used to take as long as the first solve.

    Staff who have since been removed from the roster are skipped; the
    solver reassigns their flights cold.
    """
    hints: dict[str, str] = {}
    name_lookup = {k.strip().upper(): v for k, v in name_to_id.items()}
    for r in state.allocations:
        if not r.staff_name:
            continue
        eid = name_lookup.get(r.staff_name.strip().upper())
        if not eid:
            continue
        hints[_flight_key(
            r.flt, r.dep, r.arr,
            r.std.isoformat(timespec="minutes"), r.date.isoformat(),
        )] = eid
    return hints


def _snapshot_prev_staff(state: AppState) -> dict[str, str]:
    """``{flight_key: staff_name}`` for the run that is about to be
    replaced. The Allocations tab uses it to flag rows whose staff
    changed, so the assigner can verify a re-solve only touched the
    people it was supposed to."""
    return {
        _flight_key(
            r.flt, r.dep, r.arr,
            r.std.isoformat(timespec="minutes"), r.date.isoformat(),
        ): r.staff_name
        for r in state.allocations if r.staff_name
    }


def _read_auto_p2f_handlers_from_rosters(
    am_roster_rows: list[AMRosterRow],
    availability: list[AvailabilityRow],
    d_day: date_t,
) -> dict[ShiftCode, list[str]]:
    """Walk BOTH rosters for cells whose status is P2F/M, P2F/A, or
    P2F/N on D. Returns {shift: employee_id}. First match per shift wins;
    STAFF roster is scanned before AM roster so STAFF nominations take
    precedence on conflict.

    Per user direction 2026-05-10 (extended 2026-05-11): assigners
    nominate P2F handlers via roster cells like `P2F/M` — works in
    EITHER roster file. An override still wins on conflict
    (manual nominations always trump auto-picks); previously this
    function only scanned IN_AM_Roster, which is why STAFF roster
    nominations were silently ignored.
    """
    auto: dict[ShiftCode, list[str]] = {}
    # 2026-05-26 (followup): walk availability for BOTH STAFF and ZC.
    # A regular staff in IN_Staff_Roster with `N/P2F/ZC` cell becomes
    # role=ZC + license=P2F via Step 2; if we restrict to STAFF here he
    # never gets picked as a P2F handler. Combined ZC+P2F statuses are
    # also checked alongside the legacy P2F/<shift> statuses.
    for av in availability:
        if av.date != d_day:
            continue
        if av.role not in (Role.STAFF, Role.ZC):
            continue
        shift = (
            _P2F_HANDLER_STATUS_TO_SHIFT.get(av.status)
            or _ZC_P2F_HANDLER_STATUS_TO_SHIFT.get(av.status)
        )
        if shift and av.employee_id not in auto.setdefault(shift, []):
            auto[shift].append(av.employee_id)
    for am in am_roster_rows:
        cell_status = am.status_by_date.get(d_day)
        if cell_status is None:
            continue
        # 2026-05-26: M/P2F/ZC (combined) cells also nominate the staff
        # as the shift's P2F handler. Checked alongside the legacy
        # M/P2F-style cells.
        shift = (
            _P2F_HANDLER_STATUS_TO_SHIFT.get(cell_status)
            or _ZC_P2F_HANDLER_STATUS_TO_SHIFT.get(cell_status)
        )
        if shift and am.employee_id not in auto.setdefault(shift, []):
            auto[shift].append(am.employee_id)
    return {k: v for k, v in auto.items() if v}


def _build_staff_today(
    availability: list[AvailabilityRow],
    am_roster_rows: list[AMRosterRow],
    d_day: date_t,
) -> list[StaffMember]:
    """Build StaffMember list for today's allocation.

    STAFF rows: from the availability list (Step 2 output) — the roster as
    submitted; no balance solver in between (per user direction 2026-05-11).
    ZC rows: from IN_AM_Roster — shift recovered from that day's cell.
    AM rows: dropped entirely (H13 — they don't fly).
    """
    out: list[StaffMember] = []
    seen_ids: set[str] = set()
    # 2026-05-26 (followup): admit BOTH STAFF and ZC rows from
    # availability. Step 2 sets `role=ZC` per-day when the cell is
    # `M/ZC`, `M/P2F/ZC`, etc. — even on rows originating in
    # IN_Staff_Roster. Previously this loop filtered `role != STAFF`,
    # so a regular staff put on ZC duty for the day was silently
    # dropped from staff_today. The AM-roster fallback below dedupes
    # via ``seen_ids`` so workbooks that ALSO list the same person in
    # IN_AM_Roster don't double-count.
    for r in availability:
        if r.date != d_day:
            continue
        if r.role not in (Role.STAFF, Role.ZC):
            continue
        if not r.assignable or r.current_shift is None:
            continue
        is_p2f = bool(r.license and "P2F" in r.license.upper())
        out.append(StaffMember(
            employee_id=r.employee_id,
            name=r.name,
            role=r.role,
            date=d_day,
            shift_today=r.current_shift,
            license=r.license,
            is_p2f_licensed=is_p2f,
        ))
        seen_ids.add(r.employee_id)
    # ZC + P2F-handler cells — classify PER-DAY (not whole-month).
    # Cells matching a ZC variant (M/ZC, A/ZC...) put them on that
    # shift as a ZC. Cells matching a P2F handler variant (P2F/M,
    # P2F/A, P2F/N) put them on that shift, role=ZC, with P2F license
    # auto-flagged. Per user direction 2026-05-10.
    for am in am_roster_rows:
        if am.employee_id in seen_ids:
            # Already added via the availability loop above — skip to
            # prevent double-counting when the same employee appears in
            # both rosters (or when availability was rebuilt from
            # IN_AM_Roster).
            continue
        cell_status = am.status_by_date.get(d_day)
        if cell_status is None:
            continue
        recovered_shift = _ZC_STATUS_TO_SHIFT.get(cell_status)
        license_val = am.license
        is_p2f = bool(am.license and "P2F" in am.license.upper())
        if recovered_shift is None:
            # 2026-05-26: combined ZC+P2F cell? Same shift, role=ZC,
            # is_p2f_licensed=True. The "handler" wiring is done in
            # the auto-picker; here we only need to seat them as ZC
            # with the P2F flag so eligibility passes.
            recovered_shift = _ZC_P2F_HANDLER_STATUS_TO_SHIFT.get(cell_status)
            if recovered_shift is not None:
                is_p2f = True
                license_val = (
                    license_val if license_val and "P2F" in license_val.upper()
                    else "P2F (auto from roster cell — combined ZC+P2F)"
                )
            else:
                # Maybe a P2F-handler cell — same shift, with auto-license
                recovered_shift = _P2F_HANDLER_STATUS_TO_SHIFT.get(cell_status)
                if recovered_shift is None:
                    # Today they're AM (M/IGT, A/IGT) or off — H13.
                    continue
                # Mark them P2F-licensed for the day even if the master
                # license list didn't say so — the roster cell is explicit.
                is_p2f = True
                license_val = (
                    license_val if license_val and "P2F" in license_val.upper()
                    else "P2F (auto from roster cell)"
                )
        out.append(StaffMember(
            employee_id=am.employee_id,
            name=am.name,
            role=Role.ZC,
            date=d_day,
            shift_today=recovered_shift,
            license=license_val,
            is_p2f_licensed=is_p2f,
        ))
    return out


def _to_flight_inputs(
    cleaned: list[CleanFlightRow], ops_day: date_t,
    international_airport_codes: list[str] | None = None,
) -> list[FlightInput]:
    """Convert CleanFlightRow → FlightInput, tagging awkward windows and
    international flights.

    Phase 3 (2026-05-14, INTL overhaul):
      - ``is_international`` is recomputed from config (DEP-side only —
        Change 9). Pink ``routing_via_excluded_hub`` is gone.
      - ``is_preplan_deferred`` is recomputed here from (date, std). The
        cleaned-sheet schema does NOT store this flag (no column added,
        per user direction), so when Step 3 reads back from the workbook
        we lose ``c.is_preplan_deferred``. Derivation matches Step 1:
        a flight is deferred iff date == ops_day+1 AND CUTOFF <= STD <=
        DEFERRED_UPPER.
    """
    from datetime import timedelta
    from .step1_clean_flights import CUTOFF, DEFERRED_UPPER
    d_plus_1 = ops_day + timedelta(days=1)
    intl_set = {c.upper() for c in (international_airport_codes or [])}
    out: list[FlightInput] = []
    for c in cleaned:
        dep_u = c.dep.upper()
        # Change 9: DEP-side only.
        is_intl = dep_u in intl_set
        # Change 7 redux: derive deferred from (date, std).
        is_deferred = (c.date == d_plus_1 and CUTOFF <= c.std <= DEFERRED_UPPER)
        f = FlightInput(
            date=c.date, flt=c.flt, type=c.type, ac=c.ac,
            dep=c.dep, arr=c.arr, std=c.std, load=c.load,
            ops_class=c.ops_class,
            is_international=is_intl,
            is_preplan_deferred=is_deferred,
        )
        out.append(tag_awkward_window(f, ops_day))
    return out


def _build_p2f_handler_anchors(
    flights: list[FlightInput],
    handlers_by_shift: dict[ShiftCode, list[str]],
    ops_day: date_t,
) -> dict[str, list[int]]:
    """Compute the list of P2F flight STDs (in ops-day minutes) per
    handler — drives the H4 buffer (F6 in eligibility: no normal
    flights from STD-2hrs to STD+1hr)."""
    from ..allocator.windows import is_in_std_window
    out: dict[str, list[int]] = defaultdict(list)
    for f in flights:
        if f.ops_class != OpsClass.P2F:
            continue
        std_min = std_to_ops_day_minutes(f.std, f.date, ops_day)
        # Every handler who is ELIGIBLE for this flight (their shift's STD
        # window contains it — the same test eligibility F5 applies) keeps
        # the no-normal-flight window around it. Which of them flies it is
        # the solver's call. Matching on nominal shift hours instead and
        # stopping at the first hit sent 12:31-14:30 flights to M1 and
        # 21:01-23:00 flights to A1 — shifts with no handlers — so the A
        # and N handlers who actually flew them got no buffer at all.
        for shift, handlers in handlers_by_shift.items():
            if handlers and is_in_std_window(f.std, f.date, ops_day, shift):
                for handler in handlers:
                    out[handler].append(std_min)
    return dict(out)


def run(
    state: AppState,
    d_day: date_t,
    config_path: Path | str = "configs/config.yml",
    *,
    append_warnings: bool = True,
    mode_label: str = "Step 4",
    solution_hints: dict[str, str] | None = None,
    pin_prior: bool = False,
) -> dict[str, int]:
    """Step 4 orchestrator. Reads state, runs the solver, writes back.

    Returns a count dict with FLIGHTS, ASSIGNED, UNALLOCATED, PAIRS,
    WARNINGS, and FEASIBLE for the caller to display.

    ``solution_hints`` is the previous run's ``{unique_id: employee_id}``
    map, used as a CP-SAT warm start. When None it is auto-built from
    the allocations still on the state. Hints are silently ignored if no
    longer feasible.

    ``pin_prior`` is the minimal-change re-solve used by the Unallocated
    tab's recommender buttons: every prior assignment is locked in place
    and the whole-day leveling/break post-passes are skipped, so only the
    flights that were unallocated get placed. Without it the overrides
    just loosen constraints and the solver re-optimizes the entire day.
    """
    config: Config = load_config(config_path)
    started = _time.monotonic()
    warnings: list[WarningRow] = []
    counts: dict[str, int] = {
        "FLIGHTS": 0, "ASSIGNED": 0, "UNALLOCATED": 0,
        "PAIRS": 0, "WARNINGS": 0, "FEASIBLE": 0,
    }

    # --- Read inputs ----------
    cleaned = state.allocatable_cleaned()
    availability = list(state.availability)
    overrides = list(state.overrides)
    prev_staff_by_key = _snapshot_prev_staff(state)
    prev_forced_keys = {
        _flight_key(
            r.flt, r.dep, r.arr,
            r.std.isoformat(timespec="minutes"), r.date.isoformat(),
        )
        for r in state.allocations
        if r.staff_name and "FORCED:" in (r.warning or "")
    }

    am_rows = read_library(state, "am_roster", config, [d_day])

    # Build a name -> employee_id map for resolving override rows. Names
    # come from BOTH availability (STAFF) AND the AM roster (AM/ZC).
    name_to_id: dict[str, str] = {}
    for av in availability:
        if av.date == d_day and av.name:
            name_to_id[av.name.strip().upper()] = av.employee_id
    for amr in am_rows:
        if amr.name:
            name_to_id[amr.name.strip().upper()] = amr.employee_id

    if solution_hints is None:
        solution_hints = _prior_allocations_as_hints(state, name_to_id)
        if solution_hints:
            print(
                f"  solver: auto-loaded {len(solution_hints)} warm-start "
                "hints from the previous allocation"
            )

    # Step-4-specific overrides (date is implicit from d_day).
    p2f_nominations = read_p2f_nominations(overrides, d_day, name_to_id)
    p2f_excluded_ids = read_p2f_exclusions(overrides, name_to_id)
    per_staff = read_per_staff_overrides(overrides, d_day, name_to_id)
    # Per-day "sick" pulls a staff out of today's allocation. Applied
    # below once staff_today is built — we zero their shift_today so
    # eligibility treats them as off, and the solver redistributes
    # their flights automatically. Logged so "I marked X sick but they
    # still got flights" has a diagnostic trail.
    sick_employee_ids = read_sick_overrides(overrides, d_day, name_to_id)
    if sick_employee_ids:
        print(
            f"  sick overrides: {len(sick_employee_ids)} row(s) — "
            f"{sick_employee_ids[:5]}"
            + (f" ... (+{len(sick_employee_ids) - 5} more)"
               if len(sick_employee_ids) > 5 else "")
        )
    # Per-day role change: {employee_id: "STAFF"|"ZC"|"AM"}. Applied
    # right after the sick filter; STAFF -> ZC switches the H18 bucket
    # so they sit at 14-15, AM makes them ineligible (everything they
    # were carrying redistributes via the pin re-solve).
    role_changes = read_role_change_overrides(overrides, d_day, name_to_id)
    # Phase R per-flight overrides — converted into constraint-side sets
    # below once FlightInput unique_ids are resolved.
    waive_h10_rows = read_waive_h10_pairs(overrides, d_day, name_to_id)
    raise_cap_rows = read_raise_cap_overrides(overrides, d_day, name_to_id)
    skip_p2f_buffer_rows = read_skip_p2f_buffer_overrides(
        overrides, d_day, name_to_id,
    )
    skip_intl_removal_rows = read_skip_intl_removal_overrides(overrides, d_day)

    flights = _to_flight_inputs(
        cleaned, d_day,
        international_airport_codes=config.io.sv_portal.international_airport_codes,
    )
    counts["FLIGHTS"] = len(flights)

    staff_today = _build_staff_today(availability, am_rows, d_day)

    # 2026-05-27 diagnostic: surface staff_today shape early so a
    # mis-applied override (sick / change_role / etc. flipping
    # assignable=False for everyone) is visible in the console
    # instead of producing "2263 unallocated" with no explanation.
    from collections import Counter as _Counter
    _shift_counter = _Counter(
        (s.shift_today or "OFF", s.role.value) for s in staff_today
    )
    _assignable = sum(
        1 for s in staff_today
        if s.shift_today is not None
    )
    print(
        f"  staff_today: total={len(staff_today)} on-shift={_assignable} "
        f"  flights to allocate: {len(flights)}"
    )
    for (sh, role), n in sorted(_shift_counter.items()):
        print(f"    {sh:>4} / {role:<6}: {n}")
    if _assignable == 0:
        print(
            "  !! WARNING: ZERO staff on shift today. Every flight will "
            "be UNALLOCATED. Likely cause: a `sick` / `remove_staff` row "
            "in the override list matched everyone, or every roster row has "
            "assignable=False. Check the Override drawer and the "
            "uploaded rosters."
        )

    # 2026-05-18 (user direction): apply "sick" override — flip
    # shift_today to None for every employee in the sick list. The
    # rest of the pipeline (eligibility, pair generation, solver)
    # treats them as off today, so their flights redistribute to
    # remaining staff on the same shift on the next solve. Done BEFORE
    # the per-staff overrides loop so a sick staff with a max_flights
    # override still ends up off.
    if sick_employee_ids:
        sick_set = set(sick_employee_ids)
        # Resolve names that didn't match an employee_id by scanning
        # roster names (case-insensitive, internal-whitespace-collapsed).
        # 2026-05-28: switched to the centralized _normalize_name helper
        # so 'AKSHAT  CHATURVEDI' (double-space) and 'akshat chaturvedi'
        # both resolve to the same key — previously a sick override with
        # mismatched whitespace silently failed to remove the staff.
        from .staged_overrides import _normalize_name
        name_lookup = {_normalize_name(s.name): s.employee_id for s in staff_today}
        resolved = set()
        unresolved: list[str] = []
        for sid in sick_set:
            if any(s.employee_id == sid for s in staff_today):
                resolved.add(sid)
            elif _normalize_name(sid) in name_lookup:
                resolved.add(name_lookup[_normalize_name(sid)])
            else:
                unresolved.append(sid)
        if resolved:
            sick_names = [s.name for s in staff_today if s.employee_id in resolved]
            staff_today = [
                s.model_copy(update={"shift_today": None})
                if s.employee_id in resolved else s
                for s in staff_today
            ]
            print(
                f"  Sick override: removed {len(resolved)} staff from today's "
                f"allocation: {', '.join(sick_names)}"
            )
        if unresolved:
            # Loud failure so the user knows their sick override didn't
            # take effect. Common causes: name typo, staff not on shift
            # today (already off), or no matching id in roster.
            print(
                f"  !! Sick override WARNING: {len(unresolved)} sick "
                f"row(s) could NOT be resolved to staff_today rows: "
                f"{unresolved}. These staff will still be allocated "
                "flights — fix the name/id in the Override drawer and re-run."
            )

    # 2026-05-18 (user direction): apply role-change override. Maps
    # employee_id -> new Role enum. Switching to ZC reroutes them
    # into the ZC bucket (H18 target 14-15 day, 11 N); switching to
    # AM zeros shift_today (AMs don't fly — same effect as sick);
    # promoting AM back to STAFF/ZC needs the AM roster row to have
    # carried a shift, so they were already in staff_today (the
    # _build_staff_today loop includes AMs as off-pool entries).
    if role_changes:
        name_lookup = {s.name.strip().upper(): s.employee_id for s in staff_today}
        resolved_changes: dict[str, str] = {}
        for raw_eid, new_role_str in role_changes.items():
            if any(s.employee_id == raw_eid for s in staff_today):
                resolved_changes[raw_eid] = new_role_str
            elif raw_eid.upper() in name_lookup:
                resolved_changes[name_lookup[raw_eid.upper()]] = new_role_str
        if resolved_changes:
            new_staff_today: list[StaffMember] = []
            change_log: list[str] = []
            for s in staff_today:
                new_role_str = resolved_changes.get(s.employee_id)
                if new_role_str is None:
                    new_staff_today.append(s)
                    continue
                new_role = Role(new_role_str)
                # Promoting to AM == off the live pool. STAFF<->ZC is
                # just a role swap; shift stays the same so the H18
                # bucket auto-routes them based on the new role.
                update_dict: dict = {"role": new_role}
                if new_role == Role.AM:
                    update_dict["shift_today"] = None
                new_staff_today.append(s.model_copy(update=update_dict))
                change_log.append(f"{s.name} ({s.role.value}->{new_role.value})")
            staff_today = new_staff_today
            print(
                f"  Role-change override: {len(resolved_changes)} staff "
                f"re-roled: {', '.join(change_log)}"
            )

    # Phase 3 / Change 7 (2026-05-14, INTL overhaul): D+1 flights with
    # STD ∈ [05:05, 05:30] are flagged ``is_preplan_deferred=True`` by
    # Step 1. They enter the pool but are split out HERE before the
    # solver runs — re-appended at the end as warning="PREPLAN_DEFERRED"
    # rows with empty staff column. Solver never sees them.
    preplan_only_flights: list[FlightInput] = [
        f for f in flights if f.is_preplan_deferred
    ]
    if preplan_only_flights:
        pre_ids = {f.unique_id for f in preplan_only_flights}
        flights = [f for f in flights if f.unique_id not in pre_ids]
        counts["FLIGHTS"] -= len(preplan_only_flights)
        print(
            f"  Pre-plan-deferred flights split out: "
            f"{len(preplan_only_flights)} (D+1 STD 05:05-05:30; "
            "re-appended with warning=PREPLAN_DEFERRED)"
        )

    # Apply per-staff override rows (newbie flag, max_flights,
    # std_start, std_cutoff). The cutoff_time row carries BOTH bounds
    # since 2026-05-27 — std_start is the lower bound (custom window
    # start), std_cutoff is the upper bound (custom window end).
    per_staff_today = {p.employee_id: p for p in per_staff if p.date == d_day}
    if per_staff_today:
        staff_today = [
            s.model_copy(update={
                "is_newbie": per_staff_today[s.employee_id].is_newbie or s.is_newbie,
                "max_flights_cap": per_staff_today[s.employee_id].max_flights or s.max_flights_cap,
                "std_cutoff": per_staff_today[s.employee_id].std_cutoff or s.std_cutoff,
                "std_start": per_staff_today[s.employee_id].std_start or s.std_start,
            }) if s.employee_id in per_staff_today else s
            for s in staff_today
        ]

    # --- Pre-solve capacity check (W210) ----------
    # Soft behavior (post-2026-05-10): if aggregate capacity falls short
    # of flight count, log W210 as ERROR so the assigner sees the gap,
    # but DON'T abort. The solver places what it can; the rest land in
    # UNALLOCATED. Previously this aborted entirely, which masked
    # otherwise-good runs that just had a small capacity shortfall.
    cap = aggregate_capacity(staff_today)
    if cap < len(flights):
        gap = len(flights) - cap
        warnings.append(WarningRow(
            severity=Severity.ERROR, code="W210",
            message=(
                f"aggregate capacity ({cap}) < flight count ({len(flights)}); "
                f"~{gap} flights will be UNALLOCATED. Add staff, raise H16 caps, "
                "or reduce flights."
            ),
        ))

    # --- Build eligibility context ----------
    # P2F handler resolution order (per user direction 2026-05-10):
    #   1. AUTO from roster cells P2F/M, P2F/A, P2F/N — first match per
    #      shift wins.
    #   2. override `p2f` rows — overwrite the auto-pick if the
    #      assigner explicitly nominates a different person.
    staff_by_id = {s.employee_id: s for s in staff_today}
    auto_p2f = _read_auto_p2f_handlers_from_rosters(am_rows, availability, d_day)
    # Every handler per shift, primary first. Roster cells and manual
    # nominations ADD to each other (a shift with >8 P2F flights needs
    # several); take someone off with "Not doing P2F today".
    handlers_by_shift: dict[ShiftCode, list[str]] = {}
    for _sh, _ids in auto_p2f.items():
        _keep = [e for e in _ids if e not in p2f_excluded_ids]
        if _keep:
            handlers_by_shift[_sh] = _keep
    if p2f_excluded_ids:
        print(f"  excluded from P2F today: {sorted(p2f_excluded_ids)}")
    if auto_p2f:
        print(
            f"  auto-picked P2F handlers from roster cells: "
            f"{ {k: v for k, v in auto_p2f.items()} }"
        )
    # Compute which shifts actually have P2F flights in their window —
    # those are the shifts where a missing nomination is a real problem.
    from ..allocator.windows import SHIFT_NOMINAL_MIN
    # Per user direction 2026-05-10: P2F handlers are M/A/N only.
    # M1/A1 windows are intentionally NOT counted — those P2F flights
    # have no handler and will land in OUT_Unallocated if their STD
    # doesn't also fall in an M/A/N coverage window.
    p2f_shifts_needing_handler: set[ShiftCode] = set()
    # Per-shift P2F flight count for W215 (8-flight ceiling per handler).
    p2f_count_by_shift: dict[ShiftCode, int] = defaultdict(int)
    # 2026-05-27 (centralization): pulled from schemas — single source
    # of truth for which shifts nominate plain P2F handlers.
    from ..schemas import P2F_HANDLER_ELIGIBLE_SHIFTS
    p2f_relevant_shifts = P2F_HANDLER_ELIGIBLE_SHIFTS
    from ..allocator.windows import is_in_std_window as _in_std_window
    for f in flights:
        if f.ops_class != OpsClass.P2F:
            continue
        # Same STD-window test eligibility uses, so the shift counted here
        # is the shift whose handler can actually take the flight.
        for shift in SHIFT_NOMINAL_MIN:
            if shift not in p2f_relevant_shifts:
                continue
            if _in_std_window(f.std, f.date, d_day, shift):
                p2f_shifts_needing_handler.add(shift)
                p2f_count_by_shift[shift] += 1
                break

    P2F_HANDLER_CAP = 8  # noqa: N806
    for nom in p2f_nominations:
        if nom.date != d_day:
            continue
        if nom.employee_id in p2f_excluded_ids:
            continue  # operator took this person off P2F for today
        s = staff_by_id.get(nom.employee_id)
        if s is None:
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W212", date=d_day,
                message=(
                    f"P2F nomination for shift {nom.shift}: "
                    f"employee_id {nom.employee_id} not on the roster today. "
                    "Edit the overrides or check the staff roster."
                ),
            ))
            continue
        if s.shift_today != nom.shift:
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W212", date=d_day,
                name=s.name,
                message=(
                    f"P2F nomination says {nom.employee_id} ({s.name}) "
                    f"is on {nom.shift} today, but the roster has them on "
                    f"{s.shift_today or 'OFF'}. "
                    "Edit the overrides or fix the roster."
                ),
            ))
            continue
        if not s.is_p2f_licensed:
            # 2026-05-27 (user direction): a manual P2F nomination is
            # the assigner saying "this person handles P2F today" —
            # don't reject for a missing license cell on the roster.
            # Auto-flip is_p2f_licensed for the day and emit a WARN
            # (not ERROR) so the assigner still sees the nomination
            # was made for someone the roster doesn't tag as P2F.
            print(
                f"  P2F nomination: {s.name} ({nom.shift}) — auto-granting "
                "P2F license for today (roster license cell didn't say P2F)"
            )
            staff_today = [
                _s.model_copy(update={
                    "is_p2f_licensed": True,
                    "license": (
                        _s.license if _s.license and "P2F" in _s.license.upper()
                        else "P2F (auto from override nomination)"
                    ),
                }) if _s.employee_id == nom.employee_id else _s
                for _s in staff_today
            ]
            # Refresh the local references so subsequent loop iterations
            # see the patched staff.
            staff_by_id = {_s.employee_id: _s for _s in staff_today}
            warnings.append(WarningRow(
                severity=Severity.WARN, code="W212", date=d_day,
                name=s.name,
                message=(
                    f"P2F nomination for {s.name}: roster license didn't "
                    "include 'P2F', auto-granted for today via override. "
                    "Update the roster's license cell to silence this warning."
                ),
            ))
        if nom.employee_id not in handlers_by_shift.setdefault(nom.shift, []):
            handlers_by_shift[nom.shift].append(nom.employee_id)

    # Top each shift up to the handlers it needs (one per 8 P2F flights),
    # from the roster's P2F licence column — only with someone confirmed
    # valid (STAFF, on that shift today, licence really says P2F). Roster
    # cells and manual nominations above are kept; only the shortfall is
    # filled. Anything still short is a gap the operator must fill.
    from ..allocator.p2f_autofill import (
        Candidate,
        autofill_handlers,
        handlers_needed,
    )
    _needed = {
        sh: handlers_needed(p2f_count_by_shift.get(sh, 0))
        for sh in sorted(p2f_shifts_needing_handler)
    }
    _picked = autofill_handlers(
        (Candidate(_s.employee_id, _s.name, _s.role.value,
                   _s.shift_today, _s.license) for _s in staff_today),
        _needed, handlers_by_shift, p2f_excluded_ids,
    )
    for _shift, _cands in _picked.items():
        for _c in _cands:
            handlers_by_shift.setdefault(_shift, []).append(_c.employee_id)
            print(f"  auto-filled P2F handler for {_shift} from licence: {_c.name}")

    # W215: a shift with more P2F flights than its handlers can take.
    for _shift, _need in sorted(_needed.items()):
        _have = len(handlers_by_shift.get(_shift, []))
        if _have and _have < _need:
            warnings.append(WarningRow(
                severity=Severity.WARN, code="W215", date=d_day,
                message=(
                    f"Shift {_shift} has {p2f_count_by_shift[_shift]} P2F "
                    f"flights; one handler can do max {P2F_HANDLER_CAP}, so "
                    f"{_need} handlers are needed but only {_have} found. "
                    f"Nominate {_need - _have} more P2F-licensed staff on "
                    f"{_shift} in Handler Assign."
                ),
            ))

    # Primary handler per shift (hosts pre-planning); the rest are extras.
    p2f_handler_by_shift: dict[ShiftCode, str] = {
        sh: ids[0] for sh, ids in handlers_by_shift.items() if ids
    }
    p2f_extra_handlers_by_shift: dict[ShiftCode, tuple[str, ...]] = {
        sh: tuple(ids[1:]) for sh, ids in handlers_by_shift.items()
        if len(ids) > 1
    }

    # Warn loudly for any shift that has P2F flights but no valid nomination.
    for shift in sorted(p2f_shifts_needing_handler):
        if shift not in p2f_handler_by_shift:
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W212", date=d_day,
                message=(
                    f"P2F flights exist for shift {shift} but no valid "
                    "P2F handler nomination found in the overrides. "
                    "Add a row: type=p2f_handler, date={D}, shift={shift}, "
                    "employee_id=<P2F-licensed staff on shift>."
                ),
            ))

    p2f_anchors = _build_p2f_handler_anchors(flights, handlers_by_shift, d_day)

    # ---- Phase R per-flight override sets ----
    # Build a (flt, std_iso) → unique_id resolver so override rows
    # (which key by flt+std) map onto the solver's unique_id keying.
    uid_by_flt_std: dict[tuple[str, str], str] = {
        (f.flt, f.std.isoformat(timespec="minutes")): f.unique_id
        for f in flights
    }

    def _resolve_uid(flt: str, std_iso) -> str | None:
        key = (flt, std_iso if isinstance(std_iso, str)
                  else std_iso.isoformat(timespec="minutes"))
        return uid_by_flt_std.get(key)

    # SkipP2FBuffer → set of (flight_uid, emp_id) tuples for elig F6
    skip_p2f_buffer_pairs: set[tuple[str, str]] = set()
    for r in skip_p2f_buffer_rows:
        flight_key = f"{r.flight}|{r.std.isoformat(timespec='minutes')}"
        skip_p2f_buffer_pairs.add((flight_key, r.employee_id))

    # SkipINTLRemoval → set of flight_keys (no employee dimension)
    skip_intl_removal_keys: set[str] = set()
    for r in skip_intl_removal_rows:
        skip_intl_removal_keys.add(
            f"{r.flight}|{r.std.isoformat(timespec='minutes')}"
        )

    # WaiveH10Pair → set of (uid_a, uid_b, emp_id). We need to resolve
    # both flights' unique_ids. ``other_std`` is on the SAME staff so
    # we scan their existing flights to find it.
    waive_h10_triples: set[tuple[str, str, str]] = set()
    for r in waive_h10_rows:
        uid_a = _resolve_uid(r.flight, r.std)
        if uid_a is None:
            continue
        # other flight: same staff, std = other_std; resolve by scanning
        # flights for matching std (any FLT acceptable — the OTHER side
        # is identified by STD on the same staff at the constraint
        # level, not by FLT)
        other_iso = r.other_std.isoformat(timespec="minutes")
        # Find ANY flight at that std — if the staff has multiple at
        # the same std, all pairs get the waiver.
        for f in flights:
            if f.std.isoformat(timespec="minutes") != other_iso:
                continue
            waive_h10_triples.add(
                (uid_a, f.unique_id, r.employee_id),
            )
            waive_h10_triples.add(
                (f.unique_id, uid_a, r.employee_id),  # symmetric
            )

    # RaiseCapForFlight → set of (flight_uid, emp_id)
    raise_cap_uids: set[tuple[str, str]] = set()
    for r in raise_cap_rows:
        uid = _resolve_uid(r.flight, r.std)
        if uid is None:
            continue
        raise_cap_uids.add((uid, r.employee_id))

    if any([
        skip_p2f_buffer_pairs, skip_intl_removal_keys,
        waive_h10_triples, raise_cap_uids,
    ]):
        print(
            "  Phase R overrides loaded: "
            f"waive_h10={len(waive_h10_rows)}, raise_cap={len(raise_cap_rows)}, "
            f"skip_p2f_buffer={len(skip_p2f_buffer_rows)}, "
            f"skip_intl_removal={len(skip_intl_removal_rows)}"
        )

    elig_ctx = EligibilityContext(
        ops_day=d_day,
        p2f_handler_by_shift=p2f_handler_by_shift,
        extra_p2f_handlers_by_shift=p2f_extra_handlers_by_shift,
        p2f_flight_minutes_by_handler=p2f_anchors,
        skip_p2f_buffer_pairs=frozenset(skip_p2f_buffer_pairs),
        zc_end_cutoff_min=config.zc_cutoff.minutes_before_shift_end,
        zc_start_cutoff_min=config.zc_cutoff.minutes_after_shift_start,
    )

    # --- Eligibility matrix + zero-eligibility check (W201) ----------
    # Soft behavior (post-2026-05-10): flights with zero eligibility are
    # logged as W201 and EXCLUDED from the solver's input. The remaining
    # flights still proceed. Previously this aborted the entire run.
    matrix = build_matrix(flights, staff_today, elig_ctx)
    # Keyed by FlightInput.unique_id (matches eligibility matrix output).
    zero_elig_flights = [f for f in flights if not matrix.get(f.unique_id)]
    zero_elig_reasons: dict[str, str] = {}
    for f in zero_elig_flights:
        diags = diagnose_unassignable(f, staff_today, elig_ctx)
        # If EVERY staff failed F5 (OUT_OF_SHIFT) the flight's STD lies
        # outside every shift's distribution window — surface the exact
        # message the user asked for in OUT_Unallocated (2026-05-12 §1).
        if diags and all(r.value == "STD outside shift's distribution window (user 2026-05-12)"
                         for _eid, r in diags):
            top_reason = "STD outside all shift windows"
        else:
            top_reason = diags[0][1].value if diags else "no eligible staff"
        zero_elig_reasons[f.unique_id] = top_reason
        # Phase 3 / Change 4 (2026-05-14, INTL overhaul): when an INTL DEP
        # flight is unassignable specifically because no on-shift handler
        # covers D-75 → STD, emit W216 INTL_UNASSIGNABLE (in addition to
        # the generic W201). Gives the assigner a sharper signal.
        is_intl_coverage_failure = (
            f.is_international
            and diags
            and all(
                r.name == "INTL_SHIFT_COVERAGE"
                for _eid, r in diags
            )
        )
        if is_intl_coverage_failure:
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W216", date=f.date,
                message=(
                    f"INTL flight {f.flt} (STD "
                    f"{f.std.isoformat(timespec='minutes')}, DEP {f.dep}) "
                    "is unassignable — no on-shift handler covers D-75 "
                    "through STD. Add an override or reshape the roster."
                ),
            ))
        warnings.append(WarningRow(
            severity=Severity.ERROR, code="W201", date=f.date,
            message=(
                f"flight {f.flt} (STD {f.std.isoformat(timespec='minutes')}) "
                f"has zero eligible staff — left UNALLOCATED. Top diagnoses: "
                + "; ".join(f"{eid}: {r.value}" for eid, r in diags[:3])
            ),
        ))
    if zero_elig_flights:
        zero_ids = {f.unique_id for f in zero_elig_flights}
        flights = [f for f in flights if f.unique_id not in zero_ids]
        # Drop their entries from the matrix too — solver only sees viable.
        matrix = {fid: s for fid, s in matrix.items() if fid not in zero_ids}

    # --- Pair generation (W211 on PairGenerationError) ----------
    # 2026-05-28 (Phase 2 default): generate_pairs hoists today's P2F
    # nominees to the front of M / A / N STAFF lists so they pair
    # across boundaries (Change 2), and folds orphan ZCs into the
    # STAFF pool when ZC counts mismatch (Change 3 — A↔A1 ZC pair +
    # STAFF spillover). The behavior is unconditional now — empty
    # p2f_handlers / balanced ZCs degrade gracefully.
    try:
        pairs = generate_pairs(
            staff_today, overrides=None,
            p2f_handlers=elig_ctx.p2f_handler_by_shift,
        )
        # Task 3 (2026-05-12): scan pairs for staff with multiple
        # partners at the same boundary — flag everything except the
        # sanctioned A→N split-1+2 pattern. W215 warnings flow into
        # OUT_Warnings + the dashboard panel.
        from ..allocator.pair_validation import (
            shared_planner_notes,
            validate_pair_discrepancies,
        )
        warnings.extend(validate_pair_discrepancies(pairs, d_day))
        # Next shift bigger than the previous: say who plans for two.
        warnings.extend(shared_planner_notes(pairs, d_day))
    except PairGenerationError as e:
        warnings.append(WarningRow(
            severity=Severity.ERROR, code="W211", message=str(e),
        ))
        _store_warnings(state, warnings, append=append_warnings)
        state.run_date = d_day
        state.stamp_run(
            mode=mode_label, solver_status="INFEASIBLE",
            duration_s=_time.monotonic() - started,
        )
        counts["WARNINGS"] = len(warnings)
        return counts
    counts["PAIRS"] = len(pairs)

    # --- Day-level workload target (Finding 1, 2026-05-15) ------------
    # The manual allocator picks ONE workload level per day and pins
    # almost every non-handler staff to it (everyone-at-22 on light
    # days, everyone-at-24 on heavy ones). Solver matches this by
    # computing the level from the day's flight-to-staff ratio and
    # using it as the target in S1.
    #
    # Per (shift, role): level = round(total_flights / non_handler_staff_count),
    # clipped to [band.min, band.max] from configs/shift_limits.json.
    # P2F handlers are excluded from the bucket —
    # they're already shape-shifted by their handler-specific rules.
    #
    # 2026-09-23 fix: ``total_flights`` above must be the flights that
    # fall in THAT shift's own window, not the whole day's flight
    # count. The previous version divided every bucket by the SAME
    # global ratio (len(flights) / all-shifts' staff), so a shift with
    # a much lighter night-time flight volume (e.g. N: ~169 flights /
    # 18 staff =~ 9-10 each) still got handed the day-wide ratio
    # (~21, driven by the much busier M/A shifts) before band-clipping
    # ever saw it. Clipped up to band.min, that level was unreachable
    # for anyone in the bucket, weakening S1's ability to pull the
    # bucket together (2026-09-23: N/STAFF spread of 5-11 survived a
    # 700s solve). Bucketing flights by their own shift window (same
    # windowing the solver enforces via eligibility) gives each shift
    # its own realistic ratio instead.
    #
    # 2026-10-10 fix: shifts overlap (M/M1, M1/A, A/A1, A1/N), so a flight
    # is not "owned" by one shift. Each normal flight is shared between
    # every shift whose STD window can take it (the same test eligibility
    # F5 applies), in proportion to that shift's headcount — the share
    # each shift would carry if everyone eligible did equal work. The
    # previous first-match on nominal hours credited M1 only with
    # 12:31-14:30 and A1 only with 21:01-23:00, so M1/A1 were always
    # targeted at the band floor while M/A were targeted at the cap.
    # ZCs carry a smaller band, so their expected load comes off the
    # shift's share first and STAFF level on what remains.
    from ..allocator.windows import is_in_std_window as _in_std_window
    handler_set = {e for ids in handlers_by_shift.values() for e in ids}
    bucket_sizes: dict[tuple[ShiftCode, Role], int] = defaultdict(int)
    staff_per_shift: dict[ShiftCode, int] = defaultdict(int)
    for s in staff_today:
        if (s.shift_today is None or s.role == Role.AM
                or s.employee_id in handler_set):
            continue
        bucket_sizes[(s.shift_today, s.role)] += 1
        staff_per_shift[s.shift_today] += 1
    flights_per_shift: dict[ShiftCode, float] = defaultdict(float)
    for f in flights:
        if f.ops_class == OpsClass.P2F:
            continue  # handler-only; handlers sit outside the buckets
        covering = [
            sh for sh in staff_per_shift
            if _in_std_window(f.std, f.date, d_day, sh)
        ]
        heads = sum(staff_per_shift[sh] for sh in covering)
        for sh in covering:
            flights_per_shift[sh] += staff_per_shift[sh] / heads
    from ..allocator.caps import get_band as _get_band

    def _clip(level: int, shift_code: ShiftCode, role: Role) -> int:
        band = _get_band(shift_code, role)
        if band is None:
            return level
        return max(band["min"], min(level, band["max"]))

    day_level_by_bucket: dict[tuple[ShiftCode, Role], int] = {}
    for shift_code, heads in staff_per_shift.items():
        share = flights_per_shift.get(shift_code, 0.0)
        n_zc = bucket_sizes.get((shift_code, Role.ZC), 0)
        n_staff = bucket_sizes.get((shift_code, Role.STAFF), 0)
        if n_zc:
            zc_level = _clip(round(share / heads), shift_code, Role.ZC)
            day_level_by_bucket[(shift_code, Role.ZC)] = zc_level
            share = max(0.0, share - n_zc * zc_level)
        if n_staff:
            day_level_by_bucket[(shift_code, Role.STAFF)] = _clip(
                round(share / n_staff), shift_code, Role.STAFF,
            )
    if day_level_by_bucket:
        print(
            "  Day-level targeting (shared-window share): "
            + ", ".join(
                f"{k[0]}/{k[1].value}={v} "
                f"(share={flights_per_shift.get(k[0], 0):.0f}, "
                f"staff_in_shift={staff_per_shift.get(k[0], 0)})"
                for k, v in sorted(day_level_by_bucket.items())
            )
        )

    # Staff who cannot reach their bucket's level for a reason of their
    # own — a lowered max_flights cap, a cutoff_time window, or simply too
    # few eligible flights — are left out of the bucket's spread terms.
    # Otherwise the hard spread<=2 ceiling (and the soft spread penalty)
    # drags every colleague down to that one person's count + 2, and the
    # flights the colleagues can no longer take go unallocated.
    from ..allocator.caps import hard_cap_for as _hard_cap_for
    elig_count: dict[str, int] = defaultdict(int)
    for f in flights:
        if f.ops_class == OpsClass.P2F:
            continue
        for eid in matrix.get(f.unique_id, ()):
            elig_count[eid] += 1
    spread_exempt_ids: set[str] = set()
    for s in staff_today:
        if s.shift_today is None or s.role == Role.AM:
            continue
        level = day_level_by_bucket.get((s.shift_today, s.role))
        if level is None:
            continue
        if (
            s.std_cutoff is not None
            or s.std_start is not None
            or _hard_cap_for(s) < level
            or elig_count.get(s.employee_id, 0) < level
        ):
            spread_exempt_ids.add(s.employee_id)
    if spread_exempt_ids:
        print(
            f"  Spread-exempt (own cap/window/supply limits): "
            f"{sorted(staff_by_id[e].name for e in spread_exempt_ids if e in staff_by_id)}"
        )

    # --- Sick-call iteration: pin prior allocation, redistribute only
    # the sick person's flights (2026-05-18, user direction) ---------
    # When `sick_employee_ids` is non-empty AND the console workbook
    # already has allocation rows from a prior solve, build a
    # PIN SET: every (flight_uid, employee_id) that was previously
    # assigned to a NON-sick staff. Pass to the solver — those pairs
    # are forced to x=1 (hard constraint), so only the sick person's
    # flights have free decision vars. Solver redistributes ONLY them
    # to remaining staff. Minimum disruption to the existing plan.
    pinned_assignments: dict[str, str] = {}
    # 2026-05-18: collect every employee whose prior assignments should
    # be freed for redistribution. Includes sick AND role-changed
    # staff (especially STAFF->ZC: their workload needs to drop from
    # 22 to 14-15, so extra flights MUST redistribute; and
    # STAFF/ZC->AM: all their flights become unallocated, then redist).
    free_for_redist: set[str] = set()
    if sick_employee_ids or role_changes or pin_prior:
        name_lookup = {s.name.strip().upper(): s.employee_id for s in staff_today}
        for raw in (sick_employee_ids or []):
            if any(s.employee_id == raw for s in staff_today):
                free_for_redist.add(raw)
            elif raw.upper() in name_lookup:
                free_for_redist.add(name_lookup[raw.upper()])
        for raw_eid in (role_changes or {}):
            if any(s.employee_id == raw_eid for s in staff_today):
                free_for_redist.add(raw_eid)
            elif raw_eid.upper() in name_lookup:
                free_for_redist.add(name_lookup[raw_eid.upper()])
        for uid, staff_name in prev_staff_by_key.items():
            eid = name_lookup.get(staff_name.strip().upper())
            if eid is None or eid in free_for_redist:
                continue
            # Rows force-placed last run may break H10 / H16 on purpose;
            # pinning them would make the re-solve INFEASIBLE and throw
            # the whole day to the greedy fallback. Let them move.
            if uid in prev_forced_keys:
                continue
            # Only pin if this staff is still on shift — don't lock a
            # prior allocation to someone who's now off for some other
            # reason (eligibility would reject it anyway).
            if any(s_.employee_id == eid and s_.shift_today is not None
                   for s_ in staff_today):
                pinned_assignments[uid] = eid
        if pinned_assignments:
            print(
                f"  Re-solve: pinned {len(pinned_assignments)} prior "
                f"assignment(s); only freed / unallocated flights can move"
            )

    # --- Solve ----------
    # 2026-10-10 (speed): ONE solve. The previous hard spread<=2 probe
    # ran first on a separate model with up to 300s of its own; on a full
    # day it routinely timed out without any solution and the day was
    # then solved a second time from scratch. The same goal is served by
    # the soft spread terms (S1b, 600/unit + 1,000/unit beyond 1) and the
    # rebalance post-pass, which levels every bucket to a spread of 1
    # wherever eligibility and spacing allow.
    _solve_kwargs = dict(
        ops_day=d_day,
        max_seconds=config.solver.max_seconds,
        num_workers=config.solver.num_workers,
        elig_ctx=elig_ctx,
        enable_s1_count_balance=True,
        # S3 (heavy-flight spread) used pax to identify "heavy" flights;
        # disabled per user direction 2026-05-10 — pax is reference-only.
        enable_s3_heavy_spread=False,
        # S4 (row-order tiebreak) off since 2026-10-10: at 1 x row index
        # per flight (up to ~100) it outweighed S5/S6/S7 and handed the
        # odd extra flight to the top of the roster every single day.
        # The solver is deliberately stochastic, so it bought no
        # determinism either.
        enable_s4_pair_stability=False,
        enable_s5_handover_preference=True,
        enable_s6_awkward_preference=True,
        enable_s7_zc_buffer_avoidance=True,
        # Phase 4 (2026-05-14, INTL overhaul, Changes 5 + 6): soft INTL
        # spacing penalty (15-30 min same-handler band) + fair INTL
        # distribution across handlers within each shift.
        enable_s_intl_spacing=True,
        enable_s_intl_fair=True,
        # Patch 2026-05-15: within the rush bands, prefer staff
        # whose shift isn't transitioning (±30 min of nominal start/end).
        enable_s_band_shift_stability=True,
        # Phase R per-flight overrides — empty by default, populated
        # by operator approvals via the agent chat layer.
        waive_h10_triples=frozenset(waive_h10_triples),
        raise_cap_uids=frozenset(raise_cap_uids),
        # Phase R re-solve: pass previous-run assignments as warm-start
        # hints when the caller supplies them.
        solution_hints=solution_hints,
        # Finding 1 (2026-05-15): day-level workload targeting.
        day_level_by_bucket=day_level_by_bucket,
        # 2026-05-18 sick-call iteration: pin prior allocation in place.
        pinned_assignments=pinned_assignments or None,
        spread_exempt_ids=frozenset(spread_exempt_ids),
    )
    result = solve_allocation(flights, staff_today, matrix, **_solve_kwargs)

    # --- Greedy fallback (2026-09-22) ----------
    # CP-SAT is an EXACT solver: if any combination of hard constraints
    # is mutually unsatisfiable, it reports INFEASIBLE and hands back
    # ZERO assignments for the WHOLE day — even for flights that had
    # nothing to do with the conflict (see the 2026-09-22 incident:
    # a static N/ZC workload floor with zero actual night-ops flights
    # took down all 2117 flights, not just the 3 N/ZC staff). The
    # solver-side fix (soft ZC floor) addresses THAT specific cause,
    # but as a safety net against any future/unknown infeasibility, if
    # CP-SAT comes back without a usable solution we fall back to a
    # greedy pass that only enforces the non-negotiable physical rules
    # (H1 one-staff-per-flight, H10 spacing, H16 caps) and ignores
    # every soft objective. Degraded (unbalanced) but never empty.
    if result.status not in ("OPTIMAL", "FEASIBLE") or not result.assignments:
        print(
            f"  !! CP-SAT returned status={result.status} with "
            f"{len(result.assignments)} assignment(s) — falling back to "
            "the greedy allocator (H1/H10/H16 only, no workload "
            "balancing) so flights still get placed. Review the "
            "Workload tab manually after this run."
        )
        from ..allocator.greedy_fallback import greedy_allocate
        fallback_assignments = greedy_allocate(
            flights, staff_today, matrix, ops_day=d_day,
            preassigned=pinned_assignments or None,
        )
        warnings.append(WarningRow(
            severity=Severity.ERROR, code="W299", date=d_day,
            message=(
                f"CP-SAT could not produce a solution (status="
                f"{result.status}). Used the greedy fallback instead: "
                f"{len(fallback_assignments)}/{len(flights)} flights "
                "placed WITHOUT workload balancing, handover, INTL "
                "spacing, or P2F-cap logic. Review the Workload and "
                "Warnings tabs before sending this out, and report this "
                "run so the underlying CP-SAT infeasibility gets fixed."
            ),
        ))
        result = AllocationSolverResult(
            status=f"{result.status}_GREEDY_FALLBACK",
            assignments=fallback_assignments,
            wall_clock_seconds=result.wall_clock_seconds,
        )

    feasible = result.status in ("OPTIMAL", "FEASIBLE") or "GREEDY_FALLBACK" in result.status
    counts["FEASIBLE"] = int(feasible)
    counts["ASSIGNED"] = len(result.assignments)
    # UNALLOCATED includes both flights the solver couldn't place AND the
    # zero-eligibility flights that were dropped pre-solve (W201).
    counts["UNALLOCATED"] = (
        len(flights) - len(result.assignments) + len(zero_elig_flights)
    )

    # --- Post-solve assembly ----------
    rows = assemble_allocation_rows(
        flights, staff_today, result.assignments, pairs, d_day,
    )

    # 2026-05-28 (user direction): P2F post-pass ALWAYS runs BEFORE the
    # INTL post-pass — regardless of which P2F variant is selected via
    # ``p2f_adjustment.logic_v2``. Rationale: if INTL D-75 removal goes
    # first and removes a preceding domestic flight, then P2F removal
    # might also remove the INTL itself (or vice-versa), leading to
    # double-displacements. By doing P2F first, the slots it frees are
    # accounted for when INTL D-75 evaluates its own preceding-removal
    # need.
    _p2f_logic_v2 = bool(getattr(config.p2f_adjustment, "logic_v2", False))

    def _run_intl_postpass(in_rows):
        from ..allocator.postpass_intl import apply_intl_post_pass
        out_rows, summary = apply_intl_post_pass(
            in_rows, flights, staff_today, matrix,
            elig_ctx_p2f_handlers=elig_ctx.p2f_handler_by_shift,
            d_day=d_day,
            # Phase R: per-INTL-flight opt-out from the preceding-removal.
            skip_intl_removal_keys=frozenset(skip_intl_removal_keys),
        )
        if summary.n_intl_processed:
            print(
                f"  INTL post-pass: processed={summary.n_intl_processed} "
                f"removed={summary.n_case_2b} "
                f"redistributed={summary.n_redistributed} "
                f"redist_failed={summary.n_redistribute_failed}"
            )
            for line in summary.log_lines[:20]:
                print(f"  intl: {line}")
            if len(summary.log_lines) > 20:
                print(f"  intl: ... + {len(summary.log_lines) - 20} more lines")
        return out_rows, summary

    def _run_p2f_postpass(in_rows):
        if _p2f_logic_v2:
            from ..allocator.postpass_p2f import apply_p2f_post_pass_v2
            out_rows, summary = apply_p2f_post_pass_v2(
                in_rows, flights, staff_today, matrix,
                p2f_handlers=elig_ctx.p2f_handler_by_shift,
                d_day=d_day,
            )
            tag = "P2F post-pass (v2 union window)"
        else:
            from ..allocator.postpass_p2f import apply_p2f_post_pass
            out_rows, summary = apply_p2f_post_pass(
                in_rows, flights, staff_today, matrix,
                p2f_handlers=elig_ctx.p2f_handler_by_shift,
                d_day=d_day,
                tolerance_minutes=config.p2f_adjustment.tolerance_minutes,
            )
            tag = "P2F post-pass"
        if summary.n_p2f_processed:
            print(
                f"  {tag}: processed={summary.n_p2f_processed} "
                f"removed={summary.n_removed} "
                f"redistributed={summary.n_redistributed} "
                f"redist_failed={summary.n_redistribute_failed} "
                f"anchor_shortages={summary.n_anchor_shortage}"
            )
            for line in summary.log_lines[:20]:
                print(f"  p2f: {line}")
            if len(summary.log_lines) > 20:
                print(f"  p2f: ... + {len(summary.log_lines) - 20} more lines")
        return out_rows, summary

    # 2026-05-28 (user direction): always P2F first, then INTL —
    # regardless of which P2F variant is active. Avoids the
    # double-displacement edge case described above.
    print(
        "  post-pass order: P2F"
        + (" (v2 union window)" if _p2f_logic_v2 else "")
        + " -> INTL",
    )
    def _uid_of(r):
        return (
            f"{r.flt}|{r.dep}|{r.arr}|{r.std.isoformat(timespec='minutes')}"
            f"|{r.date.isoformat()}"
        )

    _owner_before_postpass = {
        _uid_of(r): r.staff_employee_id for r in rows if r.staff_employee_id
    }
    rows, p2f_summary = _run_p2f_postpass(rows)
    rows, intl_summary = _run_intl_postpass(rows)
    # Flights the P2F / INTL post-passes took off someone: the repair
    # pass below must not hand them back.
    _postpass_removed = {
        (uid, before) for uid, before in _owner_before_postpass.items()
    } - {(_uid_of(r), r.staff_employee_id) for r in rows}

    # 2026-09-23 fix: the P2F/INTL post-passes above remove flights
    # from whoever happens to sit next to a P2F/INTL flight, not from
    # whoever currently has the most flights — so they can re-open the
    # spread the solver's H18 constraint just closed (see
    # docs/allocation.md, "Post-passes"). Run a final leveling pass:
    # while a (shift, role) bucket's most- and least-loaded staff
    # differ by more than 1, move one eligible flight from the former
    # to the latter. Every move still respects eligibility, H10
    # spacing, and hard caps — it only re-picks who among already-
    # eligible people does which flight.
    from ..allocator.postpass_rebalance import apply_rebalance_pass
    if pin_prior:
        # Minimal-change re-solve: leveling would move already-placed
        # flights between staff, which is exactly what the operator
        # asked us not to do.
        from ..allocator.postpass_rebalance import RebalanceSummary
        rebalance_summary = RebalanceSummary()
    else:
        rows, rebalance_summary = apply_rebalance_pass(
            rows, flights, staff_today, matrix,
            p2f_handlers=elig_ctx.p2f_handler_by_shift,
            d_day=d_day,
            extra_handler_ids=elig_ctx.all_p2f_handler_ids,
            exempt_ids=frozenset(spread_exempt_ids),
        )
    if rebalance_summary.n_transfers:
        print(
            f"  Rebalance pass: buckets_checked="
            f"{rebalance_summary.n_buckets_checked} "
            f"buckets_improved={rebalance_summary.n_buckets_improved} "
            f"buckets_still_stuck={rebalance_summary.n_buckets_stuck} "
            f"transfers={rebalance_summary.n_transfers}"
        )
        for line in rebalance_summary.log_lines[:20]:
            print(f"  rebalance: {line}")
        if len(rebalance_summary.log_lines) > 20:
            print(f"  rebalance: ... + {len(rebalance_summary.log_lines) - 20} more lines")

    # 2026-09-23 (user direction): floating mid-shift break per on-shift
    # staff (length from config.yml, set in the Setup sidebar),
    # soft/best-effort, via 1-for-1 swaps only — never changes
    # anyone's flight count, so it can't disturb the H18/rebalance
    # workload spread above. Runs LAST, against the final schedule.
    # 2026-10-01 (user direction): everyone except N-shift gets ONE
    # reliever (a reliever relieves only one person). Staff left without
    # one are released early instead — their last hour is cleared of
    # flights — and who that is rotates day to day. Runs BEFORE the break
    # pass, which is told not to give released staff late flights back.
    from ..storage import relief_store
    from ..allocator.relievers import plan_relief
    rows, relief_plan = plan_relief(
        rows, flights, staff_today, matrix, d_day, pairs,
        free_early_min=config.relief_pass.free_early_minutes,
        history=relief_store.history_counts(d_day),
        move_flights=not pin_prior,
    )
    relief_store.record(d_day, relief_plan.freed)
    print(
        f"  Relief pass: relieved={len(relief_plan.reliever_of)} "
        f"released_early={len(relief_plan.freed)} "
        f"uncovered={len(relief_plan.uncovered)} "
        f"swaps={relief_plan.n_swaps} transfers={relief_plan.n_transfers}"
    )
    for line in relief_plan.log_lines[:20]:
        print(f"  relief: {line}")
    _sb = {x.employee_id: x for x in staff_today}

    def _names_capped(ids, sb, cap=10):
        names = sorted(sb[i].name for i in ids)
        extra = len(names) - cap
        return ", ".join(names[:cap]) + (f" and {extra} more" if extra > 0 else "")

    if relief_plan.freed:
        warnings.append(WarningRow(
            severity=Severity.INFO, code="W240", date=d_day,
            message=(
                f"{len(relief_plan.freed)} staff have no reliever today and "
                f"were released {config.relief_pass.free_early_minutes} min "
                "before shift end (no flights in that window): "
                + _names_capped(relief_plan.freed, _sb)
                + ". Who is released rotates day to day."
            ),
        ))
    if relief_plan.uncovered:
        warnings.append(WarningRow(
            severity=Severity.WARN, code="W241", date=d_day,
            message=(
                f"{len(relief_plan.uncovered)} staff have no reliever and "
                "their last hour could not be cleared of flights: "
                + _names_capped(relief_plan.uncovered, _sb)
            ),
        ))

    from ..allocator.postpass_break import apply_break_pass
    if pin_prior:
        from ..allocator.postpass_break import BreakSummary
        break_summary = BreakSummary()
    else:
        rows, break_summary = apply_break_pass(
            rows, flights, staff_today, matrix, d_day,
            break_len=config.break_pass.length_minutes,
            max_std_by_staff=relief_plan.freed,
        )
    print(
        f"  Break pass ({config.break_pass.length_minutes} min): "
        f"considered={break_summary.n_staff_considered} "
        f"already_clear={break_summary.n_clean} "
        f"cleared_via_swap={break_summary.n_created_via_swap} "
        f"partial={break_summary.n_partial} "
        f"not_found={break_summary.n_not_found} "
        f"swaps={break_summary.n_swaps}"
    )
    for line in break_summary.log_lines[:20]:
        print(f"  break: {line}")
    if len(break_summary.log_lines) > 20:
        print(f"  break: ... + {len(break_summary.log_lines) - 20} more lines")

    # Merge all post-pass workload-note dicts (host -> list).
    merged_workload_notes: dict[str, list[str]] = defaultdict(list)
    for sid, msgs in intl_summary.workload_notes.items():
        merged_workload_notes[sid].extend(msgs)
    for sid, msgs in p2f_summary.workload_notes.items():
        merged_workload_notes[sid].extend(msgs)
    for sid, msgs in rebalance_summary.workload_notes.items():
        merged_workload_notes[sid].extend(msgs)
    for sid, msgs in break_summary.workload_notes.items():
        merged_workload_notes[sid].extend(msgs)
    for sid, msgs in relief_plan.workload_notes.items():
        merged_workload_notes[sid].extend(msgs)

    # 2026-05-26 fix: post-passes displace flights between staff via
    # ``_set_assignee``, which updates the row's staff fields but
    # never recomputes planned_by / relieved_by. So a flight moved
    # from Ravi (A→N partner = Kunal) to Priya (A→N partner = Suraj)
    # showed staff=Priya AND planned_by=Kunal — even though Kunal-
    # Priya isn't a pair. Re-derive the labels against each row's
    # CURRENT staff so the allocations match the pair map.
    # --- Zero-unallocated guarantee (operator direction 2026-10-01) ----------
    # Whatever the solver and post-passes could not place — including the
    # zero-eligibility flights dropped before the solve — still gets a
    # person. Spacing below 15 / 30 min, a cap overrun, or an ineligible
    # handler are all acceptable here; each is flagged on the row and in
    # Warnings so the assigner can see exactly what was relaxed.
    def _row_uid(r):
        return (
            f"{r.flt}|{r.dep}|{r.arr}|{r.std.isoformat(timespec='minutes')}"
            f"|{r.date.isoformat()}"
        )

    _all_flights = list(flights) + list(zero_elig_flights)

    # Self-plan rule (2026-10-16, user direction): a flight that must be
    # planned before its holder's shift starts needs their handover
    # planner; beyond what the planner covers (or with no planner at all)
    # the holder can't take it. Such flights are taken off here and
    # placed again by repair below, which respects the rule; force-place
    # relaxes it only as a last resort (flagged "no planner"). Skipped on
    # a pinned re-run, which must not move placed flights.
    from ..allocator.self_plan import SelfPlanRule, backup_planners
    # M1 helps A (2026-10-16): A flights beyond what the M planner covers
    # get an M1 planner first, so they stay where the solver put them.
    _backup = backup_planners(
        pairs, staff_today, _all_flights, d_day,
        {_row_uid(r): r.staff_employee_id for r in rows if r.staff_employee_id},
        avoid_ids=elig_ctx.all_p2f_handler_ids,
        breaks=break_summary.breaks,
    )
    if _backup:
        pairs = [*pairs, *_backup]
        counts["PAIRS"] = len(pairs)
        print(
            f"  M1 helps A: {sum(p.preplan_count for p in _backup)} early A "
            f"flight(s) planned by {len({p.prev_employee_id for p in _backup})} "
            "M1 person(s)"
        )
    from ..allocator.pair_validation import m1_help_notes
    warnings.extend(m1_help_notes(pairs, d_day))
    _self_plan = SelfPlanRule(pairs, staff_today, _all_flights, d_day)
    if not pin_prior:
        _excess = set(_self_plan.excess({
            _row_uid(r): r.staff_employee_id for r in rows if r.staff_employee_id
        }))
        if _excess:
            for r in rows:
                if _row_uid(r) in _excess:
                    merged_workload_notes[r.staff_employee_id].append(
                        f"Self-plan: FLT {r.flt} "
                        f"({r.std.isoformat(timespec='minutes')}) moved — it "
                        "would need planning before their shift starts and "
                        "their planner's flights are already covered"
                    )
            rows = [r for r in rows if _row_uid(r) not in _excess]
            print(
                f"  Self-plan rule: {len(_excess)} flight(s) taken off people "
                "with no planner for them; re-placed below"
            )

    _placed = {_row_uid(r): r.staff_employee_id for r in rows if r.staff_employee_id}
    _leftover = [f for f in _all_flights if f.unique_id not in _placed]

    # Repair (2026-10-11): before relaxing any rule, try to free an
    # eligible person for each leftover flight with a short chain of
    # moves (A takes the leftover, A's clashing flight goes to B, ...).
    # Every move obeys the hard rules; only what this cannot place goes
    # on to force-place. Skipped on a pinned re-run, which must not move
    # placed flights.
    _repaired = False
    if _leftover and not pin_prior:
        from ..allocator.eligibility import sheet_target_for
        from ..allocator.repair import repair_leftovers
        from ..allocator.postsolve import assemble_allocation_rows as _assemble
        _rep = repair_leftovers(
            _leftover, _placed, _all_flights, staff_today, matrix,
            ops_day=d_day,
            max_std_by_staff=relief_plan.freed,
            blocked_windows=break_summary.breaks,
            forbidden=_postpass_removed,
            extra_ok=_self_plan.ok,
        )
        if _rep.placed or _rep.moved:
            _repaired = True
            _sb_rep = {x.employee_id: x for x in staff_today}
            _fb_rep = {f.unique_id: f for f in _all_flights}
            new_rows = []
            for r in rows:
                uid = _row_uid(r)
                eid = _rep.moved.get(uid)
                if eid is not None:
                    s_new = _sb_rep[eid]
                    r = r.model_copy(update={
                        "staff_employee_id": eid,
                        "staff_name": s_new.name,
                        "sheet_target": sheet_target_for(_fb_rep[uid], s_new),
                    })
                elif uid in _rep.placed and not r.staff_employee_id:
                    continue   # replaced by the placed row below
                new_rows.append(r)
            new_rows.extend(_assemble(
                [_fb_rep[u] for u in _rep.placed], staff_today,
                dict(_rep.placed), pairs, d_day,
            ))
            rows = new_rows
            for sid, msgs in _rep.workload_notes.items():
                merged_workload_notes[sid].extend(msgs)
            _placed = {
                _row_uid(r): r.staff_employee_id
                for r in rows if r.staff_employee_id
            }
            _leftover = [f for f in _all_flights if f.unique_id not in _placed]
        print(
            f"  Repair pass: placed {len(_rep.placed)} of "
            f"{_rep.n_tried} leftover flight(s) without relaxing a rule "
            f"({len(_rep.moved)} other flight(s) moved to make room)"
        )
        for line in _rep.log_lines[:20]:
            print(f"  repair: {line}")

    if _leftover:
        from ..allocator.force_place import force_place_remaining
        from ..allocator.postsolve import assemble_allocation_rows as _assemble
        _forced = force_place_remaining(
            _leftover, staff_today, _placed, _all_flights,
            ops_day=d_day, elig_ctx=elig_ctx,
            max_std_by_staff=relief_plan.freed,
            self_plan=_self_plan,
        )
        _forced_assign = {fp.flight.unique_id: fp.employee_id for fp in _forced}
        _forced_rows = _assemble(
            [fp.flight for fp in _forced], staff_today, _forced_assign,
            pairs, d_day,
        )
        _note_by_uid = {fp.flight.unique_id: fp for fp in _forced}
        _prefixes = tuple(
            f"flight {fp.flight.flt} (STD "
            f"{fp.flight.std.isoformat(timespec='minutes')}) has zero eligible"
            for fp in _forced
        )
        # The W201 "left UNALLOCATED" errors are no longer true.
        warnings[:] = [
            w for w in warnings
            if not (w.code == "W201" and w.message.startswith(_prefixes))
        ]
        for fr in _forced_rows:
            fp = _note_by_uid.get(_row_uid(fr))
            if fp is None:
                continue
            tag = "FORCED: " + fp.summary
            rows.append(fr.model_copy(update={
                "warning": f"{fr.warning}; {tag}" if fr.warning else tag,
            }))
        for fp in _forced:
            if not fp.notes:
                continue   # placed without relaxing anything
            warnings.append(WarningRow(
                severity=Severity.WARN, code="W230", date=d_day,
                name=fp.staff_name,
                message=(
                    f"flight {fp.flight.flt} (STD "
                    f"{fp.flight.std.isoformat(timespec='minutes')}) was "
                    f"force-placed on {fp.staff_name} so nothing stays "
                    f"unallocated: {fp.summary}."
                ),
            ))
        print(
            f"  Force-placed {len(_forced)} leftover flight(s); "
            f"{sum(1 for fp in _forced if fp.notes)} needed a rule relaxed"
        )
        counts["FORCED"] = len(_forced)

    # Second leveling pass (2026-10-10). Force-place runs after the first
    # rebalance and hands each leftover flight to whoever breaks the
    # fewest rules — often someone already at the top of their bucket
    # (real 26-Sep run: every bucket within 2 before force-place, 3
    # after). Level again, leaving the forced rows where they are and
    # honouring early releases and secured break windows.
    if not pin_prior and (_leftover or _repaired):
        _forced_uids = {
            _row_uid(r) for r in rows if "FORCED:" in (r.warning or "")
        }
        rows, rebalance2 = apply_rebalance_pass(
            # All flights, so the zero-eligibility ones force-placed above
            # still count for spacing (they are frozen, never moved).
            rows, _all_flights, staff_today, matrix,
            p2f_handlers=elig_ctx.p2f_handler_by_shift,
            d_day=d_day,
            extra_handler_ids=elig_ctx.all_p2f_handler_ids,
            exempt_ids=frozenset(spread_exempt_ids),
            max_std_by_staff=relief_plan.freed,
            blocked_windows=break_summary.breaks,
            frozen_uids=frozenset(_forced_uids),
            extra_ok=_self_plan.ok,
        )
        if rebalance2.n_transfers:
            print(
                f"  Rebalance after force-place: "
                f"transfers={rebalance2.n_transfers}"
            )
        for sid, msgs in rebalance2.workload_notes.items():
            merged_workload_notes[sid].extend(msgs)

    from ..allocator.postsolve import relabel_pair_columns
    rows = relabel_pair_columns(
        rows, flights, staff_today, pairs, d_day, relief=relief_plan,
    )

    # W220 (2026-05-26 user direction): M-shift P2F handlers whose P2F
    # STD falls before 07:30 have their D-3hrs pre-brief window land
    # before M shift starts (04:00) — meaning no on-shift handler can
    # actually do the briefing. The previous-day N shift would
    # normally cover it via handover, but today's engine has no
    # visibility into yesterday's roster. We can't prevent the
    # assignment, but we surface it loudly so the assigner can
    # arrange a manual handover.
    #
    # Other shifts (A / N) are NOT warned: today's M can handover to
    # today's A (M→A pair), and today's A handovers to today's N
    # (A→N pair). Only M is "first of the day" with no precursor.
    from datetime import time as _time_t
    _M_P2F_WARN_LOWER = _time_t(4, 0)
    _M_P2F_WARN_UPPER = _time_t(7, 30)
    _flight_by_uid_warn = {f.unique_id: f for f in flights}
    _warned_uids: dict[str, str] = {}
    for r in rows:
        if not r.staff_employee_id:
            continue
        _uid = (
            f"{r.flt}|{r.dep}|{r.arr}|"
            f"{r.std.isoformat(timespec='minutes')}|"
            f"{r.date.isoformat()}"
        )
        _f = _flight_by_uid_warn.get(_uid)
        if _f is None or _f.ops_class != OpsClass.P2F:
            continue
        _handler = staff_by_id.get(r.staff_employee_id)
        if _handler is None or _handler.shift_today != "M":
            continue
        if not (_M_P2F_WARN_LOWER <= _f.std <= _M_P2F_WARN_UPPER):
            continue
        _short_msg = (
            f"M-shift P2F before 07:30 — D-3hrs pre-brief is before "
            "shift start (04:00); arrange manual handover from prev "
            "day N shift"
        )
        warnings.append(WarningRow(
            severity=Severity.WARN, code="W220", date=d_day,
            name=_handler.name,
            message=(
                f"P2F flight {_f.flt} STD "
                f"{_f.std.isoformat(timespec='minutes')} assigned to "
                f"M-shift handler {_handler.name}: D-3hrs pre-brief "
                f"window ({_f.std.replace(hour=max(0, _f.std.hour - 3)).isoformat(timespec='minutes')} "
                "approx) falls before M shift starts (04:00). The "
                "engine cannot place a pre-planner — manual handover "
                "from the previous day's N shift is required."
            ),
        ))
        _warned_uids[_uid] = _short_msg
    if _warned_uids:
        rows = [
            (
                r.model_copy(update={
                    "warning": (
                        f"{r.warning}; {_warned_uids[_k]}"
                        if r.warning else _warned_uids[_k]
                    ),
                })
                if (_k := (
                    f"{r.flt}|{r.dep}|{r.arr}|"
                    f"{r.std.isoformat(timespec='minutes')}|"
                    f"{r.date.isoformat()}"
                )) in _warned_uids else r
            )
            for r in rows
        ]
        print(
            f"  W220: flagged {len(_warned_uids)} early-morning M-shift "
            "P2F flight(s) — manual handover required"
        )

    # Reflect post-pass row mutations back into result.assignments so
    # the workload summary + OUT_Unallocated below see the new state.
    # Build a fresh assignments dict from the (possibly mutated) rows.
    post_assignments: dict[str, str] = {}
    for r in rows:
        if r.staff_employee_id:
            uid = (
                f"{r.flt}|{r.dep}|{r.arr}|{r.std.isoformat(timespec='minutes')}"
                f"|{r.date.isoformat()}"
            )
            post_assignments[uid] = r.staff_employee_id
    result_assignments = post_assignments

    # 2026-05-16 (user direction): pre-plan-deferred flights (D+1 STD
    # 05:05-05:30) are now ASSIGNED to an N-shift handler for pre-
    # planning. The N staff doesn't fly the leg — they just prepare
    # it during their night. Write their name in planned_by; staff
    # column stays empty (no one's flying it today). Drop the
    # PREPLAN_DEFERRED warning text now that the row carries a
    # responsible person.
    #
    # Selection: round-robin across N-shift STAFF (excludes ZC, AM,
    # P2F handler) — fair load on the prep work and
    # avoids picking the same person every night.
    if preplan_only_flights:
        from ..schemas import AllocationRow, AllocationSheet
        handler_set = elig_ctx.all_p2f_handler_ids
        n_pool = [
            s for s in staff_today
            if s.shift_today == "N"
            and s.role == Role.STAFF
            and s.employee_id not in handler_set
        ]
        n_pool.sort(key=lambda s: s.employee_id)  # deterministic order
        for i, f in enumerate(preplan_only_flights):
            preplanner = n_pool[i % len(n_pool)] if n_pool else None
            rows.append(AllocationRow(
                date=f.date, flt=f.flt, dep=f.dep, arr=f.arr,
                std=f.std, pax=f.load,
                staff_employee_id="", staff_name="",
                planned_by_employee_id=(preplanner.employee_id
                                        if preplanner else None),
                planned_by_name=(preplanner.name if preplanner else None),
                warning="",
                sheet_target=AllocationSheet.NIGHT_OPS,
                is_international=f.is_international,
            ))
    # Pass the combined post-pass reason notes (INTL §2 + P2F §3) so
    # they appear in the violations column.
    summary = build_workload_summary(
        staff_today, result_assignments,
        extra_reasons=dict(merged_workload_notes),
        # Neetu fix (2026-05-12): pass rows so counts come from the
        # exact list the writer will emit, not the reconstructed dict.
        rows=rows,
    )

    # --- Build OUT_Unallocated rows (red-tinted "needs attention" sheet)
    # Combines:
    #   (a) zero-eligibility flights dropped pre-solve (W201 — reason
    #       comes from diagnose_unassignable: e.g., 'INTL_SHIFT_EDGE')
    #   (b) flights the solver chose to leave unallocated post-solve
    #       (capacity / spacing / cap-hit — reason 'capacity or spacing')
    unallocated_rows: list[dict[str, object]] = []
    # All flights from the original list — re-fetch since we dropped some
    all_flights_orig = (
        list(flights) + zero_elig_flights
    )
    # 2026-05-18: track redistribution leftovers (sick OR role-change)
    # separately so we can emit a W219 WARN naming the affected staff
    # + reason. A flight is a "leftover" iff it was previously assigned
    # to someone whose prior assignment is no longer pinned (sick,
    # demoted to AM, or downgraded STAFF->ZC and capped) AND the
    # solver couldn't re-place it.
    sick_leftover_flights: list[tuple[str, str, str]] = []  # (flt, name, why)
    name_lookup_for_leftover = {s.employee_id: s.name for s in staff_today}
    why_by_eid: dict[str, str] = {}
    for raw in (sick_employee_ids or []):
        # resolve to canonical eid
        eid = raw
        if not any(s.employee_id == raw for s in staff_today):
            up_match = {s.name.strip().upper(): s.employee_id for s in staff_today}
            eid = up_match.get(raw.upper(), raw)
        why_by_eid[eid] = "sick"
    for raw_eid, new_role in (role_changes or {}).items():
        eid = raw_eid
        if not any(s.employee_id == raw_eid for s in staff_today):
            up_match = {s.name.strip().upper(): s.employee_id for s in staff_today}
            eid = up_match.get(raw_eid.upper(), raw_eid)
        # Use new_role to phrase the reason (STAFF->ZC is a workload
        # cap, AM is a full removal; both surface as "role-change").
        why_by_eid.setdefault(eid, f"role-change to {new_role}")
    # Build a flt->prior-staff map from the original allocations
    # sheets so we can attribute leftovers.
    prior_flt_to_staff: dict[str, str] = dict(prev_staff_by_key) if why_by_eid else {}

    for f in all_flights_orig:
        if f.unique_id in zero_elig_reasons:
            reason = zero_elig_reasons[f.unique_id]
        elif f.unique_id not in result_assignments:
            reason = "no slot — capacity or spacing constraint"
            # Was this flight previously held by a freed-for-redist
            # staff (sick or role-changed)? Re-label + queue W219.
            prev_staff = prior_flt_to_staff.get(f.unique_id, "")
            if prev_staff:
                prev_name_up = prev_staff.upper()
                matched_eid = next(
                    (eid for eid in why_by_eid
                     if name_lookup_for_leftover.get(eid, "").upper() == prev_name_up),
                    None,
                )
                if matched_eid:
                    why = why_by_eid[matched_eid]
                    reason = (
                        f"{why} redistribution failed — no eligible "
                        f"candidate within shift (was {prev_staff})"
                    )
                    sick_leftover_flights.append((f.flt, prev_staff, why))
        else:
            continue  # allocated, skip
        unallocated_rows.append({
            "date": f.date.isoformat(),
            "flt": f.flt,
            "dep": f.dep,
            "arr": f.arr,
            "std": f.std.isoformat(timespec="minutes"),
            "pax": f.load,
            "ops_class": f.ops_class.value,
            "is_international": f.is_international,
            "reason": reason,
        })
    # W219: surface the leftover count (sick OR role-change) so the
    # operator sees it on the Warnings tab without scanning
    # OUT_Unallocated. Keys = (name, why) so a single person split
    # across reasons (unlikely but possible) gets two distinct rows.
    if sick_leftover_flights:
        # defaultdict already imported at module top — re-importing
        # inside the function would shadow it as a local for the whole
        # scope (Python scoping quirk) and break earlier usages.
        by_who = defaultdict(list)
        for flt, who, why in sick_leftover_flights:
            by_who[(who, why)].append(flt)
        for (who_name, why), flts in by_who.items():
            warnings.append(WarningRow(
                severity=Severity.WARN, code="W219", date=d_day,
                name=who_name,
                message=(
                    f"{why}: {len(flts)} flight(s) from {who_name} "
                    f"could not be redistributed within their shift — "
                    f"left in OUT_Unallocated for manual handling. "
                    f"Flights: {', '.join(str(x) for x in flts[:10])}"
                    + (f" +{len(flts) - 10} more" if len(flts) > 10 else "")
                ),
            ))

    bundle = AllocationResult(
        rows=tuple(rows),
        pairs=tuple(pairs),
        summary=tuple(summary),
        warnings=tuple(warnings),
        wall_clock_seconds=result.wall_clock_seconds,
        solver_status=result.status,
    )

    # --- Phase R: write recommender sidecar JSON ----------
    # For each unallocated flight, compute up to 3 ranked override
    # suggestions and stash them in a JSON sidecar next to the console
    # workbook. The web UI reads this via GET /api/recommendations and
    # surfaces inline Approve buttons under each Unallocated row.
    #
    # Include the zero-eligibility flights (stripped from `flights` /
    # `matrix` before solve) so the recommender's Path-B / Path-C
    # branches can suggest skip_p2f_buffer / skip_intl_removal for
    # them. Each gets an empty matrix entry so Path A is a no-op.
    from ..allocator.recommender import recommend_for_unallocated
    try:
        reco_flights = list(flights) + list(zero_elig_flights)
        reco_matrix = dict(matrix)
        for f in zero_elig_flights:
            reco_matrix.setdefault(f.unique_id, set())
        recs = recommend_for_unallocated(
            flights=reco_flights,
            staff_today=staff_today,
            matrix=reco_matrix,
            assignments=result_assignments,
            elig_ctx=elig_ctx,
            ops_day=d_day,
        )
        rec_payload = {
            "ops_day": d_day.isoformat(),
            "n_unallocated": len(recs),
            "recommendations": [
                {
                    "flight_id": r.flight_id,
                    "flt": r.flt,
                    "dep": r.dep,
                    "arr": r.arr,
                    "std": r.std_iso,
                    "is_international": r.is_international,
                    "suggestions": [
                        {
                            "intrusiveness": s.intrusiveness,
                            "type": s.override_type,
                            "rationale": s.rationale,
                            "payload": s.payload,
                            "target_staff": s.target_staff_name,
                        }
                        for s in r.suggestions
                    ],
                }
                for r in recs
            ],
        }
        with state.lock:
            state.recommendations = rec_payload
        if recs:
            print(f"  Recommender: {len(recs)} unallocated flight(s)")
    except Exception as e:  # noqa: BLE001
        # Recommender is advisory — never block the main pipeline.
        print(f"  Recommender skipped: {type(e).__name__}: {e}")

    # --- Write outputs ----------
    with state.lock:
        state.run_date = d_day
        state.allocations = list(bundle.rows)
        state.allocation_stale = False
        state.pairs = list(bundle.pairs)
        state.workload = list(bundle.summary)
        state.unallocated = unallocated_rows
        state.prev_staff_by_key = prev_staff_by_key
    if warnings:
        _store_warnings(state, warnings, append=append_warnings)
    # Refresh the staffing advice with what this run actually had to
    # squeeze — it must never suggest removing people from those hours.
    from . import recommender_staffing as _rs
    try:
        _rs.run_for_state(state, d_day)
    except Exception as exc:  # noqa: BLE001 — advisory only
        print(f"  WARNING: staffing recommender failed: {exc}")
    state.stamp_run(
        mode=mode_label, solver_status=result.status,
        duration_s=_time.monotonic() - started,
    )

    counts["WARNINGS"] = len(warnings)
    # Post force-placement truth, not the pre-pass solver count.
    counts["UNALLOCATED"] = len(unallocated_rows)
    counts["ASSIGNED"] = len(result_assignments)
    return counts


def _store_warnings(
    state: AppState, warnings: list[WarningRow], *, append: bool,
) -> None:
    """Put this stage's warnings on the state, either appending to what
    Plan left behind or replacing it.

    Allocate's own codes are all W2xx (Plan's are W0xx). A previous
    Allocate's W2xx rows are dropped first, so re-running Allocate (sick
    call, override, or just again) replaces them instead of piling up a
    second copy of every warning."""
    with state.lock:
        kept = [w for w in state.warnings if not w.code.startswith("W2")]
        state.warnings = kept + warnings if append else list(warnings)


