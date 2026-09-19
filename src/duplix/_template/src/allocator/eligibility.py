"""Eligibility matrix for Step 4 (round-3 update).

For every (flight, staff) pair, answer one question: is this staff eligible
to be assigned this flight, given the *hard* constraints H2, H3, H4, H11,
H12, H13? The matrix is a sparse mapping ``flight_id -> set[employee_id]``
that the CP-SAT solver consumes to define decision variables.

Each filter is a tiny pure function. Filter ordering does not change
correctness (all filters must pass — AND semantics) but it does change the
``ExcludeReason`` returned for diagnostic logs.

Hard constraints enforced HERE:
  H2   shift coverage (incl. handover overlap window — both prev and next
       shift are eligible in the (nominal_end, nominal_end+30] window;
       solver picks via S1+S5)
  H3   P2F license match
  H4   P2F dedication + 3-window buffer (D-3hrs hard exclusion;
       D-1hr / D+20min max-1)
  H11  per-day overrides (STD cutoff, max_flights via H16 cap)
  H12  international newbie + shift-edge exclusions
  H13  AM exclusion

Hard constraints NOT enforced here (deferred to the solver):
  H1   exactly one staff per flight       — solver objective
  H10  10-min spacing                     — solver IntervalVar
  H16  hard caps                          — solver constraint

Hard constraints removed in round-3 (now SOFT, handled in the solver):
  H7   handover window (was tail-ext hard)        — soft via S5
  H9   ZC report buffers (was hard exclusion)     — soft via S7
  H15  awkward routing (was hard pre-filter)      — soft via S6

Hard constraints removed entirely in round-3:
  H5   NORSE handler ±60 min buffer               — dropped
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_t
from datetime import time as time_t
from enum import StrEnum

from ..schemas import (
    AllocationSheet,
    FlightInput,
    OpsClass,
    Role,
    ShiftCode,
    StaffMember,
)
from .windows import (
    P2F_HARD_BLOCK_BEFORE_HRS,
    P2F_PARTIAL_AFTER_MIN,
    P2F_PARTIAL_BEFORE_HRS,
    P2F_WINDOW_HALF_MIN,
    SHIFT_NOMINAL_MIN,
    awkward_eligible_shifts,
    is_in_std_window,
    std_to_ops_day_minutes,
)

# D-marker offset: planning happens 75 minutes before STD.
_D75_OFFSET_MIN: int = 75


def _intl_handler_covers_d75_through_std(
    staff_shift: ShiftCode,
    flight_std: time_t,
    flight_date: date_t,
    ops_day: date_t,
) -> bool:
    """For INTL same-handler rule (Change 4): does ``staff_shift``'s
    nominal working window cover the full span from D-75 (the planning
    marker) through STD?

    Used by:
      * Phase 2 (2026-05-14) — postsolve's INTL self-pair override
        skips assigning PLANNED_BY / RELIEVED_BY when the handler can't
        actually cover the planning window. Phase 3 makes this an
        eligibility filter that rejects the assignment entirely.

    Pure function — no side effects. Returns True iff:
      * shift's nominal start ≤ (STD - 75 min), AND
      * STD ≤ shift's nominal end.

    Both bounds in ops-day minutes (handles N's midnight wrap via
    ``std_to_ops_day_minutes``)."""
    nominal = SHIFT_NOMINAL_MIN.get(staff_shift)
    if nominal is None:
        return False
    start_min, end_min = nominal
    std_min = std_to_ops_day_minutes(flight_std, flight_date, ops_day)
    d75_min = std_min - _D75_OFFSET_MIN
    return start_min <= d75_min and std_min <= end_min


class ExcludeReason(StrEnum):
    """Diagnostic tag returned when a filter rejects a (flight, staff)
    pair. Used by W201 diagnostic messages when a flight has zero
    eligible staff."""

    AM_ROLE = "AM (no flights, H13)"
    NOT_ASSIGNABLE_TODAY = "staff not on shift today"
    P2F_NOT_LICENSED = "P2F flight requires P2F license (H3)"
    P2F_NOT_HANDLER = "P2F flight only goes to shift's nominated handler (H4)"
    P2F_HANDLER_HARD_BLOCK = (
        "normal flight in handler's D-3hrs ±15min window — hard block (H4)"
    )
    NORSE_NOT_HANDLER = (
        "NORSE flight only goes to nominated NORSE handler(s) "
        "(per user direction 2026-05-10)"
    )
    OUT_OF_SHIFT = "STD outside shift's distribution window (user 2026-05-12)"
    STD_OUTSIDE_ALL_WINDOWS = "STD outside all shift windows"
    OVERRIDE_CUTOFF = "STD past staff's per-day STD cutoff (H11)"
    NEWBIE_INTERNATIONAL = "newbies excluded from international flights (H12a)"
    INTL_SHIFT_EDGE = "international: staff within ±60 min of shift start/end (H12b — retired)"
    INTL_SHIFT_COVERAGE = (
        "international DEP: handler's shift does not cover D-75 through "
        "STD (Change 4 — same handler start-to-finish)"
    )


@dataclass(frozen=True)
class EligibilityContext:
    """Runtime context used by the filters. Computed once by the
    orchestrator at preprocessing time."""

    ops_day: date_t
    p2f_handler_by_shift: dict[ShiftCode, str] = field(default_factory=dict)
    """shift -> employee_id of the P2F handler nominated for that shift.
    Per user direction 2026-05-10: only M / A / N have P2F handlers;
    M1 and A1 P2F flights drop the handler restriction (any P2F-licensed
    staff on shift can take them)."""

    p2f_flight_minutes_by_handler: dict[str, list[int]] = field(default_factory=dict)
    """employee_id -> list of P2F flight STDs (in ops-day minutes) the
    handler is dedicated to. Drives H4's three buffer windows."""

    norse_handler_ids: tuple[str, ...] = ()
    """Nominated NORSE handlers. When non-empty, NORSE flights are
    eligible ONLY for these employees (hard constraint per user direction
    2026-05-10 — all NORSE work concentrated on the nominated handler[s];
    if more than ~10 flights, multiple nominees split the load).
    Empty tuple = no nomination = NORSE flight eligible for any staff
    on shift (graceful fallback)."""

    # Phase R per-flight overrides — populated by step3 from the override list
    # rows of type ``skip_p2f_buffer``. Each entry is (flight_key,
    # employee_id) where flight_key = ``f"{flt}|{std.isoformat()}"``.
    # When present, F6 skips the P2F buffer check for that exact pair.
    skip_p2f_buffer_pairs: frozenset[tuple[str, str]] = frozenset()

    # 2026-05-26 (user direction): employee_ids of staff who hold BOTH
    # roles for the day — ZC + P2F handler (roster cell `M/P2F/ZC` or
    # any-order equivalent). These handlers still get the F6 D-3hrs
    # hard-block (real briefing time), but the solver SKIPS the
    # D-1hr / D+20min ±15 max-1 partial windows so the buffer attrition
    # doesn't compound with ZC's lower cap (14-15 day / 10-11 night).
    zc_p2f_handler_ids: frozenset[str] = frozenset()


def _in_window(target: int, anchors: list[int], offset_min: int, half_width: int) -> bool:
    """True if ``target`` is within ``half_width`` of any (anchor + offset)."""
    return any(abs(target - (a + offset_min)) <= half_width for a in anchors)


def check(
    flight: FlightInput, staff: StaffMember, ctx: EligibilityContext,
) -> ExcludeReason | None:
    """Return the first failing exclusion reason, or None if eligible."""
    # F1: AM exclusion (H13). Cheapest; AMs never fly.
    if staff.role == Role.AM:
        return ExcludeReason.AM_ROLE
    # F2: shift assignability. Off today => not eligible.
    if staff.shift_today is None:
        return ExcludeReason.NOT_ASSIGNABLE_TODAY
    # F3: P2F license (H3). P2F flights require P2F-licensed staff.
    if flight.ops_class == OpsClass.P2F and not staff.is_p2f_licensed:
        return ExcludeReason.P2F_NOT_LICENSED
    # F4: P2F dedication (H4). P2F is strictly M / A / N only — per
    # user direction 2026-05-10, M1 and A1 staff are NEVER eligible for
    # P2F flights regardless of license. Among M/A/N staff, only the
    # shift's nominated handler is eligible.
    if flight.ops_class == OpsClass.P2F:
        if staff.shift_today not in ("M", "A", "N"):
            return ExcludeReason.P2F_NOT_HANDLER  # M1/A1 excluded
        nominated = ctx.p2f_handler_by_shift.get(staff.shift_today)
        if nominated != staff.employee_id:
            return ExcludeReason.P2F_NOT_HANDLER
    # F4b: NORSE handler check.
    #  - Per user direction 2026-05-12: Night-shift staff are NEVER
    #    eligible for NORSE.
    #  - Per user direction 2026-05-24: A1-shift staff are ALSO
    #    excluded by default. The user can still pin a specific A1
    #    person via an override of type=norse — in that case the
    #    is_norse_handler check below takes precedence over this rule.
    #  - Exactly ONE NORSE handler is nominated per cycle; that person
    #    takes every NORSE flight. The fallback path in step3 fills
    #    this even when the assigner didn't pick anyone explicitly.
    #  - The handler ALSO takes regular flights normally — NORSE
    #    flights just don't count toward their regular cap.
    is_norse_handler = staff.employee_id in ctx.norse_handler_ids
    if (flight.ops_class == OpsClass.NORSE
            and staff.shift_today in ("N", "A1")
            and not is_norse_handler):
        return ExcludeReason.NORSE_NOT_HANDLER
    if (flight.ops_class == OpsClass.NORSE and ctx.norse_handler_ids
            and not is_norse_handler):
        return ExcludeReason.NORSE_NOT_HANDLER
    # F5: STD distribution window (user direction 2026-05-12, §1).
    # A flight is eligible for a shift only if its STD falls in that
    # shift's distribution window (M / M1 / N hard-bounded; A and A1
    # have a soft outer band that the solver penalizes via S_outer).
    # NORSE handlers are STILL exempt for NORSE flights — they can
    # pick up NORSE outside any window (handler-dedicated mode).
    skip_window_check = flight.ops_class == OpsClass.NORSE and is_norse_handler
    if not skip_window_check and not is_in_std_window(
        flight.std, flight.date, ctx.ops_day, staff.shift_today,
    ):
        return ExcludeReason.OUT_OF_SHIFT
    # F6: P2F handler hard-block window (H4 round-3 rewrite).
    # For each P2F flight the handler runs, the ±15 min window around
    # (P2F_STD - 3 hrs) is a HARD exclusion: handler can't take any normal
    # flights there (briefing/paperwork time). The other two windows
    # (D-1hr ±15, D+20min ±15) are partial — encoded in the solver as
    # max-1 constraints, not as eligibility filters here.
    #
    # Phase R override: if the (flight_key, employee_id) pair is in
    # ``ctx.skip_p2f_buffer_pairs``, skip the buffer check entirely.
    if flight.ops_class != OpsClass.P2F:
        anchors = ctx.p2f_flight_minutes_by_handler.get(staff.employee_id, [])
        if anchors:
            flight_key = f"{flight.flt}|{flight.std.isoformat(timespec='minutes')}"
            override_active = (
                (flight_key, staff.employee_id) in ctx.skip_p2f_buffer_pairs
            )
            if not override_active:
                target = std_to_ops_day_minutes(
                    flight.std, flight.date, ctx.ops_day,
                )
                if _in_window(
                    target, anchors,
                    offset_min=-P2F_HARD_BLOCK_BEFORE_HRS * 60,
                    half_width=P2F_WINDOW_HALF_MIN,
                ):
                    return ExcludeReason.P2F_HANDLER_HARD_BLOCK
    # F7: per-staff STD window (H11).
    # 2026-05-27: extended to a windowed constraint — flights must fall
    # in [std_start, std_cutoff] when those are set. Either bound may be
    # None, in which case the staff's normal shift bound applies for
    # that side (i.e., no extra constraint).
    if staff.std_cutoff is not None and flight.std > staff.std_cutoff:
        return ExcludeReason.OVERRIDE_CUTOFF
    if staff.std_start is not None and flight.std < staff.std_start:
        return ExcludeReason.OVERRIDE_CUTOFF
    # F8: international newbie exclusion (H12a).
    if flight.is_international and staff.is_newbie:
        return ExcludeReason.NEWBIE_INTERNATIONAL
    # F9 (Phase 3, Change 4 — 2026-05-14): INTL DEP same-handler
    # start-to-finish. For an INTL flight, the handler's shift must
    # nominally cover D-75 through STD (strict). If not, exclude.
    # No buffer, no fallback (per user direction). Replaces the retired
    # F9 INTL_SHIFT_EDGE rule.
    if flight.is_international and not _intl_handler_covers_d75_through_std(
        staff.shift_today, flight.std, flight.date, ctx.ops_day,
    ):
        return ExcludeReason.INTL_SHIFT_COVERAGE
    return None


def build_matrix(
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    ctx: EligibilityContext,
) -> dict[str, set[str]]:
    """Return ``{flight.unique_id: {employee_id, ...}}``. Sparse: only
    includes eligible pairs.

    Per user direction 2026-05-12: keyed by ``unique_id`` (flt + dep +
    arr + STD) so multi-leg rotations with the same FLT number stay
    independent. Earlier code keyed by ``flt`` alone, which caused
    later legs to silently overwrite earlier ones in the dict.

    Flight rows with empty sets are still emitted; the orchestrator
    scans for these and aborts with W201 before invoking the solver.
    """
    matrix: dict[str, set[str]] = {}
    for f in flights:
        eligible: set[str] = set()
        for s in staff_today:
            if check(f, s, ctx) is None:
                eligible.add(s.employee_id)
        matrix[f.unique_id] = eligible
    return matrix


def diagnose_unassignable(
    flight: FlightInput, staff_today: list[StaffMember], ctx: EligibilityContext,
) -> list[tuple[str, ExcludeReason]]:
    """For a flight with zero eligible staff, return the list of
    (employee_id, reason) tuples explaining why each candidate was excluded.
    Drives W201 diagnostic messages.
    """
    out: list[tuple[str, ExcludeReason]] = []
    for s in staff_today:
        reason = check(flight, s, ctx)
        if reason is not None:
            out.append((s.employee_id, reason))
    return out


def p2f_partial_block_anchors(
    handler_p2f_flight_minutes: list[int],
) -> tuple[list[int], list[int]]:
    """Return the partial-block window centers (D-1hr and D+20min anchors)
    for the solver to encode as max-1-flight constraints.

    Result: (centers_d_minus_1hr, centers_d_plus_20min). The solver will
    iterate each list and add a constraint like
    ``Σ_f (x[f, handler] for f in 30-min window around center) ≤ 1``.
    """
    return (
        [a - P2F_PARTIAL_BEFORE_HRS * 60 for a in handler_p2f_flight_minutes],
        [a + P2F_PARTIAL_AFTER_MIN for a in handler_p2f_flight_minutes],
    )


# ---------- preprocessing helpers ----------

def tag_awkward_window(flight: FlightInput, ops_day: date_t) -> FlightInput:
    """Tag a flight with is_awkward_window + awkward_eligible_shifts for
    the solver's soft S6 routing preference. No longer drives eligibility
    (awkward routing is soft now), but the solver consults these tags to
    add a small penalty for non-preferred shifts."""
    awk = awkward_eligible_shifts(flight.std, flight.date, ops_day)
    if awk is None:
        return flight
    return flight.model_copy(update={
        "is_awkward_window": True,
        "awkward_eligible_shifts": awk,
    })


def sheet_target_for(flight: FlightInput, staff: StaffMember) -> AllocationSheet:
    """Resolve which allocation lane the row belongs to. Routing is
    by ops_class (NORSE / P2F) with the day/night split decided by the
    assigned STAFF's shift (clarification 1)."""
    if flight.ops_class == OpsClass.NORSE:
        return AllocationSheet.NORSE
    if flight.ops_class == OpsClass.P2F:
        return AllocationSheet.P2F
    return (
        AllocationSheet.NIGHT_OPS
        if staff.shift_today == "N"
        else AllocationSheet.DAY_OPS
    )
