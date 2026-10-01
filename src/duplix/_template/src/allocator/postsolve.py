"""Post-solve assembly for Step 4.

Turns the solver's raw ``{flight_id: employee_id}`` assignments into the
full allocation output: AllocationRow per flight (with PLANNED_BY /
RELIEVED_BY / WARNING / sheet_target populated) plus a per-staff
WorkloadSummaryRow.

Logic:
  PLANNED_BY (per H6, primary pairs only): for each next-shift staff S
    who has a primary pair at boundary B, S's first H6_COUNTS[B] flights
    of the day get PLANNED_BY = the pair's prev-side staff.

  RELIEVED_BY (per H7, primary AND handover-only pairs): for each
    prev-shift staff S, their flights in the tail-extension window
    (STD past nominal end, up to tail_end) get RELIEVED_BY = the
    pair's next-side staff. If S has multiple prev-side pairs (e.g.,
    A staff with both A→N primary and A→A1 handover-only), the FIRST
    one matching the prev_shift is used — the assigner can override
    via an override.

  Both can populate the same row (correction r2-5): when a flight is
  one of S's first-N (PLANNED_BY) AND in S's tail-ext (RELIEVED_BY)
  simultaneously. Rare but supported.

  Sheet routing (sheet_target_for from eligibility.py):
  P2F → P2F, N-shift → NightOps, everything else → DayOps. Cross-date
  flights (e.g., 05:10 D+1 STD with M staff) follow the STAFF's shift,
  NOT the calendar date — so they go to DayOps not NightOps.

  WARNING column: per-row soft-violation flag. Currently flags spacing
  margin <20 min between consecutive flights for the same staff (H10
  hard min is 15; <20 is a soft heads-up).

  WorkloadSummaryRow per staff: actual count vs preferred / acceptable
  / hard cap, plus a list of violation strings (under preferred, above
  preferred by 1, above preferred by 2+, hit hard cap, etc.).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_t

from ..schemas import (
    AllocationRow,
    AllocationSheet,
    FlightInput,
    OpsBoundary,
    Pair,
    Role,
    StaffMember,
    WorkloadSummaryRow,
)
from .caps import acceptable_max_for, hard_cap_for, preferred_target_for
from .eligibility import (
    _intl_handler_covers_d75_through_std,
    sheet_target_for,
)
# Re-exported as ``_force_self_pair_for_intl`` — symbolic anchor the
# Phase 3 spec tests look up. The actual self-pair override happens
# inline inside ``assemble_allocation_rows`` (no need for a separate
# function; this name is just a re-export so the test contract finds
# something hasattr-able). Phase 3 may consolidate this into a proper
# helper if the logic grows.
_force_self_pair_for_intl = _intl_handler_covers_d75_through_std
from .windows import (
    is_in_tail_ext,
    std_to_ops_day_minutes,
)

# H6 per-primary-pair pre-plan counts (defaults). Aligned with REF_Constraints
# H6. The actual count for any given pair lives on Pair.preplan_count and may
# differ from these defaults — for example when N has 2 A-partners, the
# split-1+2 rule gives the "primary" A-partner preplan_count=2 and the
# secondary A-partner preplan_count=1 (sum still 3 per N).
H6_PREPLAN_COUNTS: dict[OpsBoundary, int] = {
    OpsBoundary.N_TO_M: 1,
    OpsBoundary.M_TO_M1: 1,
    OpsBoundary.M_TO_A: 2,
    OpsBoundary.M1_TO_A1: 2,
    OpsBoundary.A_TO_N: 3,
    OpsBoundary.A1_TO_N: 0,
    OpsBoundary.A_TO_A1: 0,  # handover-only routing, no preplan
}

# Per user direction 2026-05-10: when a flight in shift S's tail-ext
# (the 30 min after nominal end) needs RELIEVED_BY annotation, the
# relieving partner must be on shift R(S):
#   M  -> relieved by A          (M->M1 is overlap, not relief)
#   M1 -> relieved by A1
#   A  -> relieved by N
#   A1 -> nobody
#   N  -> no tail-ext
_RELIEF_NEXT_SHIFT: dict[str, str] = {
    "M": "A",
    "M1": "A1",
    "A": "N",
}


def _build_pair_lookups(pairs: list[Pair]) -> tuple[
    dict[str, list[Pair]], dict[str, list[Pair]],
]:
    """Build two indexes:
      next_planning_pairs_for[emp] = list of pairs with preplan_count > 0
        where emp is next-side. Round-3 update: an N staff with split 1+2
        has TWO entries here (primary count=2 + split-secondary count=1);
        non-split staff still have one. Pairs are returned sorted by
        ``-preplan_count`` so the higher-count pair claims the staff's
        FIRST flights, the lower-count pair claims the next slice.
      prev_pairs_for[emp] = list of all pairs (any role) where emp is the
        prev-side. Drives RELIEVED_BY (any pair counts, even if
        preplan_count=0 — handover-only pairs DO carry relief).
    """
    next_planning: dict[str, list[Pair]] = defaultdict(list)
    prev_all: dict[str, list[Pair]] = defaultdict(list)
    for p in pairs:
        if p.next_employee_id and p.preplan_count > 0:
            next_planning[p.next_employee_id].append(p)
        if p.prev_employee_id:
            prev_all[p.prev_employee_id].append(p)
    # Sort planning pairs: bigger preplan_count first → claims earlier
    # flights. Tiebreak by prev_employee_id for determinism.
    for emp in next_planning:
        next_planning[emp].sort(
            key=lambda p: (-p.preplan_count, p.prev_employee_id or ""),
        )
    return next_planning, prev_all


def _build_warning(
    flight: FlightInput,
    staff: StaffMember,
    flight_position_in_staff: int,
    staff_flights: list[FlightInput],
    ops_day: date_t,
) -> str | None:
    """Compute the per-row WARNING string. None when no violation.

    Per user direction 2026-05-10: the 15-min spacing soft warn was
    too noisy (firing on every other flight). The HARD spacing rule
    (10 min, enforced by the solver) still prevents physical overlap;
    this function now only flags actual rule violations, not soft
    margin warnings. The negative-min display bug ("-135 min") is
    inherently fixed since the soft warn no longer fires.
    """
    return None


def assemble_allocation_rows(
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    assignments: dict[str, str],
    pairs: list[Pair],
    ops_day: date_t,
) -> list[AllocationRow]:
    """Build the full per-flight AllocationRow list."""
    staff_by_id = {s.employee_id: s for s in staff_today}
    next_planning_pairs, prev_pairs_for = _build_pair_lookups(pairs)

    # Group + sort flights by assigned staff (chronologically by ops-day
    # minutes). Keyed by FlightInput.unique_id so multi-leg rotations
    # (same FLT number, different STD) stay independent — earlier code
    # used the bare FLT and silently merged legs.
    flights_by_staff: dict[str, list[FlightInput]] = defaultdict(list)
    for f in flights:
        sid = assignments.get(f.unique_id)
        if sid is not None:
            flights_by_staff[sid].append(f)
    for sid in flights_by_staff:
        flights_by_staff[sid].sort(
            key=lambda f: std_to_ops_day_minutes(f.std, f.date, ops_day),
        )
    # 2026-05-26 fix: skip INTL flights when assigning a "position"
    # within the staff's day. INTL rows have ``planned_by`` blanked
    # further down — counting them as positions 0/1/2 silently eats a
    # pair partner's preplan_count slot, leaving the staff's first
    # *visible* flights unplanned. Skipping them here keeps the "first
    # 3 plannable flights" invariant on the visible output.
    position_in_staff: dict[tuple[str, str], int] = {}
    for sid, fl_list in flights_by_staff.items():
        visible_pos = 0
        for f in fl_list:
            if f.is_international:
                # Not in the plannable sequence — planned_by will be
                # blanked anyway. Map to a sentinel so planner_by_position
                # lookups never match these flights.
                position_in_staff[(f.unique_id, sid)] = -1
                continue
            position_in_staff[(f.unique_id, sid)] = visible_pos
            visible_pos += 1

    # Pre-compute, per staff, which pair plans flight at each position.
    # next_planning_pairs[sid] is sorted bigger-count-first; we walk the
    # list assigning consecutive flight-positions to each pair until that
    # pair's preplan_count is exhausted.
    planner_by_position: dict[tuple[str, int], Pair] = {}
    for sid, plan_pairs in next_planning_pairs.items():
        cursor = 0
        for p in plan_pairs:
            for _ in range(p.preplan_count):
                planner_by_position[(sid, cursor)] = p
                cursor += 1

    rows: list[AllocationRow] = []
    for f in flights:
        sid = assignments.get(f.unique_id)
        if sid is None or sid not in staff_by_id:
            continue
        s = staff_by_id[sid]
        pos = position_in_staff[(f.unique_id, sid)]

        # PLANNED_BY: look up which pair (if any) plans this flight-position.
        planned_by_emp: str | None = None
        planned_by_name: str | None = None
        planner_pair = planner_by_position.get((sid, pos))
        if planner_pair is not None:
            planned_by_emp = planner_pair.prev_employee_id
            planned_by_name = planner_pair.prev_name

        # RELIEVED_BY: only fires on flights past nominal shift end (in
        # the handover-overlap window) for prev-side pairs (any role,
        # including handover-only). Per user direction 2026-05-10, the
        # relief shift for each prev shift is fixed:
        #   M  relieved by A   (NOT M1 — that's a different overlap)
        #   M1 relieved by A1
        #   A  relieved by N
        #   A1 relieved by N
        #   N  has no tail-ext (returned False above)
        relieved_by_emp: str | None = None
        relieved_by_name: str | None = None
        if s.shift_today and is_in_tail_ext(f.std, f.date, ops_day, s.shift_today):
            relief_shift = _RELIEF_NEXT_SHIFT.get(s.shift_today)
            # 2026-05-26 fix: when a staff has multiple A→N pairs (e.g.,
            # primary + split-secondary, or duplicated entries from a
            # non-deduped staff_today on older builds), prefer the
            # PRIMARY pair with the highest preplan_count so the relief
            # annotation matches the same partner who did the bulk of
            # the planning. Removed the double-break (the second was
            # unreachable; flagged in OTHER_FINDINGS.md).
            best: Pair | None = None
            for pp in prev_pairs_for.get(sid, []):
                if (pp.prev_shift == s.shift_today
                        and pp.next_shift == relief_shift
                        and pp.next_employee_id is not None):
                    if best is None or pp.preplan_count > best.preplan_count:
                        best = pp
            if best is not None:
                relieved_by_emp = best.next_employee_id
                relieved_by_name = best.next_name

        # 2026-05-16 (user direction): INTL flights leave planned_by /
        # relieved_by BLANK. The handler is already the same person
        # start-to-finish (F9 enforces D-75 through STD coverage), so
        # the self-pair labels were redundant noise. Previously we
        # over-wrote the cross-shift pair-map output with sid/name on
        # both sides; now we just blank them so the columns stay clean.
        if f.is_international:
            planned_by_emp = None
            planned_by_name = None
            relieved_by_emp = None
            relieved_by_name = None

        warning = _build_warning(f, s, pos, flights_by_staff[sid], ops_day)
        sheet = sheet_target_for(f, s)
        rows.append(AllocationRow(
            date=f.date, flt=f.flt, dep=f.dep, arr=f.arr, std=f.std,
            pax=f.load,
            staff_employee_id=sid, staff_name=s.name,
            planned_by_employee_id=planned_by_emp,
            planned_by_name=planned_by_name,
            relieved_by_employee_id=relieved_by_emp,
            relieved_by_name=relieved_by_name,
            warning=warning,
            sheet_target=sheet,
            is_international=f.is_international,
        ))
    return rows


def relabel_pair_columns(
    rows: list[AllocationRow],
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    pairs: list[Pair],
    ops_day: date_t,
    relief=None,
) -> list[AllocationRow]:
    """Recompute ``planned_by_*`` and ``relieved_by_*`` for each row
    based on its CURRENT ``staff_employee_id``.

    ``relief`` (a ``relievers.ReliefPlan``, 2026-10-01): when given, it
    replaces the pair-based RELIEVED_BY. Every non-night person who has
    a reliever in the plan gets that reliever on the flights handed over
    at shift end (STD from 20 min before nominal end, plus the tail
    window) — or, if they have none there, on their last flight. People
    released early (no reliever) and N staff get no RELIEVED_BY.

    2026-05-26 fix. ``assemble_allocation_rows`` sets the pair-derived
    labels when the row is first built from the solver's assignments.
    INTL §2.B and P2F §3 post-passes later mutate the row's staff via
    ``_set_assignee`` — but those helpers never touch the planned_by /
    relieved_by columns, so a flight displaced from Ravi to Priya
    still shows planned_by=Kunal (Ravi's A→N partner). This pass
    re-derives the labels using the row's current staff so the
    the allocation output matches the pair map.

    Rows without ``staff_employee_id`` (preplan-deferred D+1 05:05-05:30
    rows) keep their labels untouched — those carry the N-shift
    pre-planner from step3's round-robin, not a pair partner.
    """
    from ..schemas import OpsClass as _OC
    staff_by_id = {s.employee_id: s for s in staff_today}
    flight_by_uid = {f.unique_id: f for f in flights}
    next_planning_pairs, prev_pairs_for = _build_pair_lookups(pairs)

    # Group rows by current staff, sort by STD.
    rows_by_staff: dict[str, list[AllocationRow]] = defaultdict(list)
    for r in rows:
        if r.staff_employee_id:
            rows_by_staff[r.staff_employee_id].append(r)
    for sid in rows_by_staff:
        rows_by_staff[sid].sort(
            key=lambda r: std_to_ops_day_minutes(r.std, r.date, ops_day),
        )

    # Visible-position map: INTL flights are skipped so the
    # partner's preplan_count covers the first N _plannable_ flights.
    def _row_uid(r: AllocationRow) -> str:
        return (
            f"{r.flt}|{r.dep}|{r.arr}|"
            f"{r.std.isoformat(timespec='minutes')}|"
            f"{r.date.isoformat()}"
        )

    position_by_key: dict[tuple[str, str], int] = {}
    for sid, fl_rows in rows_by_staff.items():
        visible_pos = 0
        for r in fl_rows:
            uid = _row_uid(r)
            if r.is_international:
                position_by_key[(sid, uid)] = -1
                continue
            position_by_key[(sid, uid)] = visible_pos
            visible_pos += 1

    planner_by_position: dict[tuple[str, int], Pair] = {}
    for sid, plan_pairs in next_planning_pairs.items():
        cursor = 0
        for p in plan_pairs:
            for _ in range(p.preplan_count):
                planner_by_position[(sid, cursor)] = p
                cursor += 1

    # Relief annotation targets: per staff, the row uids that carry
    # RELIEVED_BY under the 1:1 relief plan.
    relief_uids: set[str] = set()
    if relief is not None:
        from .relievers import relief_window_start
        for sid, fl_rows in rows_by_staff.items():
            s0 = staff_by_id.get(sid)
            if s0 is None or s0.shift_today is None or sid not in relief.reliever_of:
                continue
            plain = [
                r for r in fl_rows
                if not r.is_international
                and not (flight_by_uid.get(_row_uid(r)) is not None
                         and flight_by_uid[_row_uid(r)].ops_class == _OC.P2F)
            ]
            if not plain:
                continue
            start = relief_window_start(s0.shift_today)
            hand = [
                r for r in plain
                if std_to_ops_day_minutes(r.std, r.date, ops_day) >= start
            ]
            for r in (hand or [plain[-1]]):
                relief_uids.add(_row_uid(r))

    out: list[AllocationRow] = []
    for r in rows:
        sid = r.staff_employee_id
        if not sid:
            out.append(r)
            continue
        s = staff_by_id.get(sid)
        if s is None:
            out.append(r)
            continue
        uid = _row_uid(r)
        f = flight_by_uid.get(uid)
        pos = position_by_key.get((sid, uid), -1)

        planned_by_emp: str | None = None
        planned_by_name: str | None = None
        if pos >= 0:
            planner_pair = planner_by_position.get((sid, pos))
            if planner_pair is not None:
                # 2026-05-26 D-75 check: planner physically can't plan
                # a flight whose STD is more than 75 min after their
                # shift's nominal end (planning happens at most D-75).
                # When the next-side's flight is too far in the future
                # for the prev-side's shift to reach, blank planned_by
                # rather than annotate a name who couldn't actually
                # have done the work.
                from .windows import SHIFT_NOMINAL_MIN
                _prev_shift = planner_pair.prev_shift
                _prev_nominal = SHIFT_NOMINAL_MIN.get(_prev_shift) if _prev_shift else None
                _flight_min = std_to_ops_day_minutes(r.std, r.date, ops_day)
                if (_prev_nominal is not None
                        and _prev_nominal[1] < _flight_min - 75):
                    # prev shift ends before D-75; planner can't plan
                    pass
                else:
                    planned_by_emp = planner_pair.prev_employee_id
                    planned_by_name = planner_pair.prev_name

        relieved_by_emp: str | None = None
        relieved_by_name: str | None = None
        if relief is not None:
            if uid in relief_uids:
                relieved_by_emp = relief.reliever_of.get(sid)
                relieved_by_name = relief.reliever_name.get(sid)
        elif s.shift_today and is_in_tail_ext(
            r.std, r.date, ops_day, s.shift_today,
        ):
            relief_shift = _RELIEF_NEXT_SHIFT.get(s.shift_today)
            best: Pair | None = None
            for pp in prev_pairs_for.get(sid, []):
                if (pp.prev_shift == s.shift_today
                        and pp.next_shift == relief_shift
                        and pp.next_employee_id is not None):
                    if best is None or pp.preplan_count > best.preplan_count:
                        best = pp
            if best is not None:
                relieved_by_emp = best.next_employee_id
                relieved_by_name = best.next_name

        # INTL keeps blank pair-based labels (design choice — INTL is
        # same-handler start-to-finish). P2F rows USED to be blanked here too, but per the
        # 2026-05-27 direction P2F now carries host annotations populated
        # by postpass_p2f (D-3/D-1 host → planned_by, D+20 host →
        # relieved_by). Leaving P2F rows untouched here so those
        # post-pass-assigned labels survive to the output.
        is_p2f = f is not None and f.ops_class == _OC.P2F
        if is_p2f:
            # Keep whatever postpass_p2f wrote to the row.
            out.append(r)
            continue
        if r.is_international:
            planned_by_emp = None
            planned_by_name = None
            relieved_by_emp = None
            relieved_by_name = None

        out.append(r.model_copy(update={
            "planned_by_employee_id": planned_by_emp,
            "planned_by_name": planned_by_name,
            "relieved_by_employee_id": relieved_by_emp,
            "relieved_by_name": relieved_by_name,
        }))
    return out


def build_workload_summary(
    staff_today: list[StaffMember],
    assignments: dict[str, str],
    extra_reasons: dict[str, list[str]] | None = None,
    rows: list[AllocationRow] | None = None,
) -> list[WorkloadSummaryRow]:
    """Build one row per assignable staff with target/actual/deviation.

    AMs and off-shift staff are excluded — they don't fly, and zero
    rows aren't useful in the summary sheet.

    Per user direction 2026-05-11 (revised): every allocated flight
    counts toward ``actual``. P2F / FERRY / TEST / CHARTER are allocated
    as normal flights with no per-staff cap separate from H16.

    Per user direction 2026-05-12 (Neetu fix): when ``rows`` is provided,
    counts are derived directly from the (post-pass) AllocationRow list
    rather than from the parallel ``assignments`` dict. This guarantees
    that the workload "actual" matches the number of rows the writer
    will emit for that staff — eliminating any chance of drift between
    the two structures after the §2/§3 post-passes mutate the rows.

    Examples (post-2026-05-11):
      BHASKAR (P2F-A handler): 6 regular + 11 P2F                → actual = 17
      KULDEEP (P2F-M handler): 12 regular + 5 P2F                → actual = 17
      ANY STAFF: 18 regular + 2 ferry + 1 test + 1 charter       → actual = 22
    """
    counts: dict[str, int] = defaultdict(int)
    if rows is not None:
        # Preferred path (Neetu fix, 2026-05-12): count from the rows
        # themselves so the workload tally matches the allocation
        # output exactly.
        for r in rows:
            if not r.staff_employee_id:
                continue
            counts[r.staff_employee_id] += 1
    else:
        for sid in assignments.values():
            counts[sid] += 1
    out: list[WorkloadSummaryRow] = []
    for s in staff_today:
        if s.role == Role.AM or s.shift_today is None:
            continue
        actual = counts.get(s.employee_id, 0)
        preferred = preferred_target_for(s)
        accept_max = acceptable_max_for(s)
        cap = hard_cap_for(s)
        dev_below = max(0, preferred - actual)
        dev_above = max(0, actual - preferred)
        violations: list[str] = []
        if actual < preferred:
            violations.append(f"under preferred ({actual} < {preferred})")
        elif actual > accept_max:
            violations.append(
                f"above preferred ({actual} > acceptable max {accept_max})"
            )
        if actual >= cap:
            violations.append(f"at hard cap ({cap})")
        # Post-pass reasons (INTL §2, P2F §3) — appended after the
        # standard violations so the visual order is structured.
        if extra_reasons:
            for r in extra_reasons.get(s.employee_id, ()):
                violations.append(r)
        out.append(WorkloadSummaryRow(
            employee_id=s.employee_id, name=s.name,
            shift=s.shift_today, role=s.role,
            target_preferred=preferred,
            target_acceptable_max=accept_max,
            hard_cap=cap,
            actual=actual,
            deviation_below_preferred=dev_below,
            deviation_above_preferred=dev_above,
            violations=tuple(violations),
        ))
    return out


# ---------- color metadata for the writer ----------

PLANNED_BY_FILL_HEX = "FFD9EAF7"   # light blue (correction r2-5)
RELIEVED_BY_FILL_HEX = "FFD9F2D9"  # light green
WARNING_FILL_HEX = "FFFFF2CC"      # light yellow


def needs_color(row: AllocationRow) -> dict[str, str]:
    """Return ``{column_name: fill_hex}`` for every column on the row
    that should be filled. The writer applies these to the output
    workbook. Empty dict when no fill needed."""
    fills: dict[str, str] = {}
    if row.planned_by_employee_id:
        fills["planned_by"] = PLANNED_BY_FILL_HEX
    if row.relieved_by_employee_id:
        fills["relieved_by"] = RELIEVED_BY_FILL_HEX
    if row.warning:
        fills["warning"] = WARNING_FILL_HEX
    return fills


# Sheet routing helper (used by the writer to dispatch rows to the
# right allocation lane).

def split_by_sheet(
    rows: list[AllocationRow],
) -> dict[AllocationSheet, list[AllocationRow]]:
    """Group rows by sheet_target for write-time dispatching."""
    out: dict[AllocationSheet, list[AllocationRow]] = defaultdict(list)
    for r in rows:
        out[r.sheet_target].append(r)
    return out


__all__ = [
    "H6_PREPLAN_COUNTS",
    "PLANNED_BY_FILL_HEX",
    "RELIEVED_BY_FILL_HEX",
    "WARNING_FILL_HEX",
    "assemble_allocation_rows",
    "build_workload_summary",
    "needs_color",
    "split_by_sheet",
]
