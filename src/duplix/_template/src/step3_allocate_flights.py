"""Step 4 — flight allocation orchestrator.

Reads from state:
  cleaned flights (every ops class except GULF)  — what there is to allocate
  availability                                   — who is on shift for D
  overrides                                      — per-day operator intent
  the uploaded AM roster                         — AM/ZC rows (ZC shift recovery)

Builds:
  StaffMember per assignable staff (STAFF + ZC; AM excluded)
  EligibilityContext (P2F handler + flight times; NORSE handler)
  Sparse eligibility matrix
  Pair list (deterministic from row order; secondary handover-only when
    A surplus exists; split-1+2 at A→N; N→M cross-role for excess M-ZCs)

Solves with CP-SAT (hard constraints H1, H10, H16; H4 partial-block;
S1+S3+S4+S5+S6+S7 enabled).

Writes back to state:
  allocations (each row tagged with its DayOps / NightOps / P2F / NORSE lane)
  pairs, workload summary, unallocated list
  warnings (W201/W210/W211 surfaced; appended to the Plan stage's)
  recommendations for whatever could not be placed
  the run-status stamp
"""

from __future__ import annotations

import time as _time
from collections import defaultdict
from datetime import date as date_t
from datetime import time as time_t
from datetime import timedelta as _td
from pathlib import Path

from .allocator.caps import aggregate_capacity
from .allocator.eligibility import (
    EligibilityContext,
    build_matrix,
    diagnose_unassignable,
    tag_awkward_window,
)
from .allocator.pairings import PairGenerationError, generate_pairs
from .allocator.postsolve import (
    assemble_allocation_rows,
    build_workload_summary,
)
from .allocator.windows import std_to_ops_day_minutes
from .config import Config, load_config
from .io.readers import (
    read_norse_handlers,
    read_p2f_nominations,
    read_per_staff_overrides,
    read_raise_cap_overrides,
    read_role_change_overrides,
    read_sick_overrides,
    read_skip_intl_removal_overrides,
    read_skip_p2f_buffer_overrides,
    read_waive_h10_pairs,
)
from .io.roster_library import read_library
from .state import AppState
from .schemas import (
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
from .solver.allocator_cpsat import AllocationSolverResult, solve_allocation

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
from .schemas import (
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
) -> dict[ShiftCode, str]:
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
    auto: dict[ShiftCode, str] = {}
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
        if shift and shift not in auto:
            auto[shift] = av.employee_id
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
        if shift and shift not in auto:
            auto[shift] = am.employee_id
    return auto


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
    p2f_handler_by_shift: dict[ShiftCode, str],
    ops_day: date_t,
) -> dict[str, list[int]]:
    """Compute the list of P2F flight STDs (in ops-day minutes) per
    handler — drives the H4 partial-block windows in the solver and
    the F6 hard exclusion in eligibility."""
    out: dict[str, list[int]] = defaultdict(list)
    # Group P2F flights by their shift's handler.
    for f in flights:
        if f.ops_class != OpsClass.P2F:
            continue
        # The handler is whichever shift owns this STD — but we don't
        # know shift here without a staff member. Best heuristic: use
        # the shift whose nominal window contains the STD.
        from .allocator.windows import SHIFT_NOMINAL_MIN
        std_min = std_to_ops_day_minutes(f.std, f.date, ops_day)
        for shift, (start, end) in SHIFT_NOMINAL_MIN.items():
            if start <= std_min <= end:
                handler = p2f_handler_by_shift.get(shift)
                if handler:
                    out[handler].append(std_min)
                break
    return dict(out)


def run(
    state: AppState,
    d_day: date_t,
    config_path: Path | str = "configs/config.yml",
    *,
    append_warnings: bool = True,
    mode_label: str = "Step 4",
    solution_hints: dict[str, str] | None = None,
) -> dict[str, int]:
    """Step 4 orchestrator. Reads state, runs the solver, writes back.

    Returns a count dict with FLIGHTS, ASSIGNED, UNALLOCATED, PAIRS,
    WARNINGS, and FEASIBLE for the caller to display.

    ``solution_hints`` is the previous run's ``{unique_id: employee_id}``
    map, used as a CP-SAT warm start. When None it is auto-built from
    the allocations still on the state. Hints are silently ignored if no
    longer feasible.
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
    norse_handlers = read_norse_handlers(overrides, d_day, name_to_id)
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
    # NORSE handlers come from overrides only (no roster shorthand).
    staff_by_id = {s.employee_id: s for s in staff_today}
    auto_p2f = _read_auto_p2f_handlers_from_rosters(am_rows, availability, d_day)
    p2f_handler_by_shift: dict[ShiftCode, str] = dict(auto_p2f)
    if auto_p2f:
        print(
            f"  auto-picked P2F handlers from roster cells: "
            f"{ {k: v for k, v in auto_p2f.items()} }"
        )
    # Compute which shifts actually have P2F flights in their window —
    # those are the shifts where a missing nomination is a real problem.
    from .allocator.windows import SHIFT_NOMINAL_MIN
    # Per user direction 2026-05-10: P2F handlers are M/A/N only.
    # M1/A1 windows are intentionally NOT counted — those P2F flights
    # have no handler and will land in OUT_Unallocated if their STD
    # doesn't also fall in an M/A/N coverage window.
    p2f_shifts_needing_handler: set[ShiftCode] = set()
    # Per-shift P2F flight count for W215 (8-flight ceiling per handler).
    p2f_count_by_shift: dict[ShiftCode, int] = defaultdict(int)
    # 2026-05-27 (centralization): pulled from schemas — single source
    # of truth for which shifts nominate plain P2F handlers.
    from .schemas import P2F_HANDLER_ELIGIBLE_SHIFTS
    p2f_relevant_shifts = P2F_HANDLER_ELIGIBLE_SHIFTS
    for f in flights:
        if f.ops_class != OpsClass.P2F:
            continue
        std_min = std_to_ops_day_minutes(f.std, f.date, d_day)
        for shift, (start, end) in SHIFT_NOMINAL_MIN.items():
            if shift not in p2f_relevant_shifts:
                continue
            if start <= std_min <= end:
                p2f_shifts_needing_handler.add(shift)
                p2f_count_by_shift[shift] += 1
                break

    # W215: shifts with > 8 P2F flights need an additional nominee.
    P2F_HANDLER_CAP = 8  # noqa: N806
    for shift, count in sorted(p2f_count_by_shift.items()):
        if count > P2F_HANDLER_CAP:
            extra_needed = -(-count // P2F_HANDLER_CAP) - 1  # ceil-1
            warnings.append(WarningRow(
                severity=Severity.WARN, code="W215", date=d_day,
                message=(
                    f"Shift {shift} has {count} P2F flights; one handler "
                    f"can do max {P2F_HANDLER_CAP}. Nominate "
                    f"{extra_needed} additional P2F handler(s) for {shift} "
                    f"in the Override drawer (type=p2f, shift={shift}, employee=<name>)."
                ),
            ))
    for nom in p2f_nominations:
        if nom.date != d_day:
            continue
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
        p2f_handler_by_shift[nom.shift] = nom.employee_id

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

    p2f_anchors = _build_p2f_handler_anchors(flights, p2f_handler_by_shift, d_day)

    # NORSE handler nomination — user direction 2026-05-12 (Task 6):
    # exactly ONE person holds every NORSE flight for the allocation
    # cycle. Night-shift staff are NEVER eligible regardless of role.
    # If no nominee is given, pick a random non-N ZC as the handler.
    # If multiple nominees are given, the first valid one wins and the
    # extras are dropped with a W213 warning.
    norse_handler_ids: list[str] = []
    has_norse_flights = any(f.ops_class == OpsClass.NORSE for f in flights)
    p2f_handler_set = set(p2f_handler_by_shift.values())
    extra_dropped: list[str] = []
    for nom in norse_handlers:
        if nom.date != d_day:
            continue
        s = staff_by_id.get(nom.employee_id)
        if s is None:
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W213", date=d_day,
                message=(
                    f"NORSE nomination employee_id {nom.employee_id} "
                    "not on roster today."
                ),
            ))
            continue
        if s.shift_today is None:
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W213", date=d_day,
                name=s.name,
                message=(
                    f"NORSE nominee {s.name} is OFF today. "
                    "Pick someone who's on shift."
                ),
            ))
            continue
        if s.shift_today == "N":
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W213", date=d_day,
                name=s.name,
                message=(
                    f"NORSE nominee {s.name} is on Night shift. "
                    "Night-shift staff are never eligible for NORSE — "
                    "pick someone on M/A/M1/A1."
                ),
            ))
            continue
        if nom.employee_id in p2f_handler_set:
            warnings.append(WarningRow(
                severity=Severity.WARN, code="W213", date=d_day,
                name=s.name,
                message=(
                    f"NORSE nominee {s.name} is also a P2F handler today. "
                    "Pick a different person — handlers should not double up."
                ),
            ))
            continue
        if norse_handler_ids:
            extra_dropped.append(s.name)
            continue
        norse_handler_ids.append(nom.employee_id)
    if extra_dropped:
        warnings.append(WarningRow(
            severity=Severity.WARN, code="W213", date=d_day,
            message=(
                "Multiple NORSE nominees — only the first is used. "
                f"Dropped: {', '.join(extra_dropped)}. NORSE is now "
                "single-handler per user direction 2026-05-12."
            ),
        ))

    norse_count = sum(1 for f in flights if f.ops_class == OpsClass.NORSE)
    # 2026-05-18 (user direction, revised): one NORSE handler, all
    # NORSE flights go to them — even D+1 night tail and even when
    # two NORSE STDs overlap. If H10 spacing blocks one, accept the
    # unallocated; the operator handles those manually. No
    # auto-pick-extras logic.
    if has_norse_flights and not norse_handler_ids:
        import random as _rand
        eligible_zc = [
            s for s in staff_today
            if s.role == Role.ZC
            and s.shift_today is not None
            and s.shift_today != "N"
            and s.employee_id not in p2f_handler_set
        ]
        if eligible_zc:
            rng = _rand.Random(d_day.isoformat())
            pick = rng.choice(eligible_zc)
            norse_handler_ids.append(pick.employee_id)
            warnings.append(WarningRow(
                severity=Severity.WARN, code="W213", date=d_day,
                name=pick.name,
                message=(
                    "No NORSE handler nominated — auto-picked "
                    f"{pick.name} ({pick.shift_today}/ZC) at random "
                    "from non-N ZCs. Nominate explicitly via the "
                    "override drawer to lock the choice."
                ),
            ))
        else:
            warnings.append(WarningRow(
                severity=Severity.ERROR, code="W213", date=d_day,
                message=(
                    "NORSE flights exist but no non-Night ZC is on "
                    "shift to take them. Nominate an M/A/M1/A1 ZC."
                ),
            ))

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

    # 2026-05-26 (user direction): any P2F handler whose role is ZC
    # gets the partial-buffer relaxation. Source covers both roster-
    # driven (`M/P2F/ZC` cell auto-pick) and override-driven
    # nominations of a ZC as P2F handler — relaxation applies either
    # way. F6 D-3hrs hard block still applies.
    zc_p2f_handler_ids: set[str] = set()
    for _shift, _handler_id in p2f_handler_by_shift.items():
        _s = staff_by_id.get(_handler_id)
        if _s is not None and _s.role == Role.ZC:
            zc_p2f_handler_ids.add(_handler_id)
    if zc_p2f_handler_ids:
        _names = ", ".join(
            staff_by_id[eid].name for eid in sorted(zc_p2f_handler_ids)
            if eid in staff_by_id
        )
        print(
            f"  Combined ZC+P2F handlers: {len(zc_p2f_handler_ids)} "
            f"({_names}) — partial buffer windows (D-1hr / D+20min) "
            "skipped so the ZC cap isn't double-penalised."
        )

    elig_ctx = EligibilityContext(
        ops_day=d_day,
        p2f_handler_by_shift=p2f_handler_by_shift,
        p2f_flight_minutes_by_handler=p2f_anchors,
        norse_handler_ids=tuple(norse_handler_ids),
        skip_p2f_buffer_pairs=frozenset(skip_p2f_buffer_pairs),
        zc_p2f_handler_ids=frozenset(zc_p2f_handler_ids),
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
        from .allocator.pair_validation import validate_pair_discrepancies
        warnings.extend(validate_pair_discrepancies(pairs, d_day))
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
    # using it as the target in S1. See MANUAL_PATTERNS_VS_ENGINE.md
    # Finding 1 for evidence.
    #
    # Per (shift, role): level = round(total_flights / non_handler_staff_count),
    # clipped to [band.min, band.max] from configs/shift_limits.json.
    # Handlers (P2F + NORSE nominated) are excluded from the bucket —
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
    from .allocator.windows import SHIFT_NOMINAL_MIN as _SHIFT_NOMINAL_MIN
    flights_per_shift_min: dict[ShiftCode, int] = defaultdict(int)
    for f in flights:
        f_std_min = std_to_ops_day_minutes(f.std, f.date, d_day)
        for shift_code_iter, (s_start, s_end) in _SHIFT_NOMINAL_MIN.items():
            if s_start <= f_std_min <= s_end:
                flights_per_shift_min[shift_code_iter] += 1
                break
    handler_set = set(p2f_handler_by_shift.values()) | set(norse_handler_ids)
    bucket_sizes: dict[tuple[ShiftCode, Role], int] = defaultdict(int)
    staff_per_shift: dict[ShiftCode, int] = defaultdict(int)
    for s in staff_today:
        if (s.shift_today is None or s.role == Role.AM
                or s.employee_id in handler_set):
            continue
        bucket_sizes[(s.shift_today, s.role)] += 1
        staff_per_shift[s.shift_today] += 1
    from .allocator.caps import get_band as _get_band
    day_level_by_bucket: dict[tuple[ShiftCode, Role], int] = {}
    for (shift_code, role), size in bucket_sizes.items():
        shift_staff_count = staff_per_shift.get(shift_code) or 1
        raw_level = round(
            flights_per_shift_min.get(shift_code, 0) / shift_staff_count
        )
        band = _get_band(shift_code, role)
        if band is None:
            day_level_by_bucket[(shift_code, role)] = raw_level
            continue
        lo, hi = band["min"], band["max"]
        day_level_by_bucket[(shift_code, role)] = max(lo, min(raw_level, hi))
    if day_level_by_bucket:
        print(
            "  Day-level targeting (per-shift ratio, 2026-09-23 fix): "
            + ", ".join(
                f"{k[0]}/{k[1].value}={v} "
                f"(flights_in_shift={flights_per_shift_min.get(k[0], 0)}, "
                f"staff_in_shift={staff_per_shift.get(k[0], 0)})"
                for k, v in sorted(day_level_by_bucket.items())
            )
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
    if sick_employee_ids or role_changes:
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
            # Only pin if this staff is still on shift — don't lock a
            # prior allocation to someone who's now off for some other
            # reason (eligibility would reject it anyway).
            if any(s_.employee_id == eid and s_.shift_today is not None
                   for s_ in staff_today):
                pinned_assignments[uid] = eid
        if pinned_assignments:
            print(
                f"  Sick re-solve: pinned {len(pinned_assignments)} prior "
                f"assignment(s); only sick staff's flights free to move"
            )

    # --- Solve ----------
    # 2026-09-23: try a HARD same-bucket spread ceiling of 2 first
    # (user direction — "give that person a break" once they're more
    # than 2 behind their least-loaded peer). If the day's real H10
    # spacing / eligibility shape makes that impossible somewhere,
    # CP-SAT reports the WHOLE day INFEASIBLE (one combined model —
    # see hard_bucket_spread_max docstring), so we retry without the
    # hard cap rather than falling all the way through to the greedy
    # fallback, which would drop every soft objective for the entire
    # day just because one bucket couldn't hit spread<=2.
    _solve_kwargs = dict(
        ops_day=d_day,
        max_seconds=config.solver.max_seconds,
        elig_ctx=elig_ctx,
        enable_s1_count_balance=True,
        # S3 (heavy-flight spread) used pax to identify "heavy" flights;
        # disabled per user direction 2026-05-10 — pax is reference-only.
        enable_s3_heavy_spread=False,
        enable_s4_pair_stability=True,
        enable_s5_handover_preference=True,
        enable_s6_awkward_preference=True,
        enable_s7_zc_buffer_avoidance=True,
        # Phase 4 (2026-05-14, INTL overhaul, Changes 5 + 6): soft INTL
        # spacing penalty (15-30 min same-handler band) + fair INTL
        # distribution across handlers within each shift.
        enable_s_intl_spacing=True,
        enable_s_intl_fair=True,
        # Patch 2026-05-15: within the H10-relaxed bands, prefer staff
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
    )
    result = solve_allocation(
        flights, staff_today, matrix,
        hard_bucket_spread_max=2,
        **{
            **_solve_kwargs,
            # 2026-09-23: give the hard-capped attempt a SHORT probe
            # budget rather than the full config, so a bucket that
            # can't hit spread<=2 fails fast instead of burning the
            # whole time budget twice (once to fail here, again to
            # solve properly below). If it's achievable, CP-SAT
            # usually finds it well inside this window; if not, we
            # want to find that out quickly and move on.
            "max_seconds": max(60, min(config.solver.max_seconds // 2, 300)),
        },
    )
    # Any non-usable status here (INFEASIBLE, TIMEOUT/UNKNOWN,
    # MODEL_INVALID) means we do NOT have a real assignment set —
    # only OPTIMAL/FEASIBLE do. Retry without the hard cap, with the
    # FULL configured time budget, rather than falling through to the
    # greedy fallback (which would drop every soft objective for the
    # whole day just because one bucket couldn't hit spread<=2).
    if result.status not in ("OPTIMAL", "FEASIBLE"):
        print(
            f"  Hard spread<=2 probe returned {result.status} — "
            "retrying without the hard cap (soft spread penalty only, "
            "full time budget)."
        )
        result = solve_allocation(
            flights, staff_today, matrix,
            hard_bucket_spread_max=None,
            **_solve_kwargs,
        )

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
        from .allocator.greedy_fallback import greedy_allocate
        fallback_assignments = greedy_allocate(
            flights, staff_today, matrix, ops_day=d_day,
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
        from .allocator.postpass_intl import apply_intl_post_pass
        out_rows, summary = apply_intl_post_pass(
            in_rows, flights, staff_today, matrix,
            elig_ctx_p2f_handlers=elig_ctx.p2f_handler_by_shift,
            elig_ctx_norse_handlers=set(elig_ctx.norse_handler_ids),
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
            from .allocator.postpass_p2f import apply_p2f_post_pass_v2
            out_rows, summary = apply_p2f_post_pass_v2(
                in_rows, flights, staff_today, matrix,
                p2f_handlers=elig_ctx.p2f_handler_by_shift,
                norse_handlers=set(elig_ctx.norse_handler_ids),
                d_day=d_day,
            )
            tag = "P2F post-pass (v2 union window)"
        else:
            from .allocator.postpass_p2f import apply_p2f_post_pass
            out_rows, summary = apply_p2f_post_pass(
                in_rows, flights, staff_today, matrix,
                p2f_handlers=elig_ctx.p2f_handler_by_shift,
                norse_handlers=set(elig_ctx.norse_handler_ids),
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
    rows, p2f_summary = _run_p2f_postpass(rows)
    rows, intl_summary = _run_intl_postpass(rows)

    # 2026-09-23 fix: the P2F/INTL post-passes above remove flights
    # from whoever happens to sit next to a P2F/INTL flight, not from
    # whoever currently has the most flights — so they can re-open the
    # spread the solver's H18 constraint just closed (see
    # docs/REVIEW_NOTES.md finding #4). Run a final leveling pass:
    # while a (shift, role) bucket's most- and least-loaded staff
    # differ by more than 1, move one eligible flight from the former
    # to the latter. Every move still respects eligibility, H10
    # spacing, and hard caps — it only re-picks who among already-
    # eligible people does which flight.
    from .allocator.postpass_rebalance import apply_rebalance_pass
    rows, rebalance_summary = apply_rebalance_pass(
        rows, flights, staff_today, matrix,
        p2f_handlers=elig_ctx.p2f_handler_by_shift,
        norse_handlers=set(elig_ctx.norse_handler_ids),
        d_day=d_day,
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

    # 2026-09-23 (user direction): floating 45-min break per on-shift
    # staff, soft/best-effort, via 1-for-1 swaps only — never changes
    # anyone's flight count, so it can't disturb the H18/rebalance
    # workload spread above. Runs LAST, against the final schedule.
    from .allocator.postpass_break import apply_break_pass
    rows, break_summary = apply_break_pass(rows, flights, staff_today, matrix, d_day)
    print(
        f"  Break pass: considered={break_summary.n_staff_considered} "
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

    # 2026-05-26 fix: post-passes displace flights between staff via
    # ``_set_assignee``, which updates the row's staff fields but
    # never recomputes planned_by / relieved_by. So a flight moved
    # from Ravi (A→N partner = Kunal) to Priya (A→N partner = Suraj)
    # showed staff=Priya AND planned_by=Kunal — even though Kunal-
    # Priya isn't a pair. Re-derive the labels against each row's
    # CURRENT staff so the allocations match the pair map.
    from .allocator.postsolve import relabel_pair_columns
    rows = relabel_pair_columns(rows, flights, staff_today, pairs, d_day)

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
    # P2F handler, NORSE handler) — fair load on the prep work and
    # avoids picking the same person every night.
    if preplan_only_flights:
        from .schemas import AllocationRow, AllocationSheet
        handler_set = (
            set(elig_ctx.p2f_handler_by_shift.values())
            | set(elig_ctx.norse_handler_ids)
        )
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
    # Pass flights so the summary can exclude P2F + NORSE from the
    # workload count (per user direction 2026-05-10). Also pass the
    # combined post-pass reason notes (INTL §2 + P2F §3) so they
    # appear in the violations column.
    summary = build_workload_summary(
        staff_today, result_assignments,
        flights=list(flights) + zero_elig_flights,
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
    from .allocator.recommender import recommend_for_unallocated
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
        state.pairs = list(bundle.pairs)
        state.workload = list(bundle.summary)
        state.unallocated = unallocated_rows
        state.prev_staff_by_key = prev_staff_by_key
    if warnings:
        _store_warnings(state, warnings, append=append_warnings)
    state.stamp_run(
        mode=mode_label, solver_status=result.status,
        duration_s=_time.monotonic() - started,
    )

    counts["WARNINGS"] = len(warnings)
    return counts


def _store_warnings(
    state: AppState, warnings: list[WarningRow], *, append: bool,
) -> None:
    """Put this stage's warnings on the state, either appending to what
    Plan left behind or replacing it."""
    with state.lock:
        state.warnings = (
            list(state.warnings) + warnings if append else list(warnings)
        )


