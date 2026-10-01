"""Per-unallocated-flight override recommender (Phase R).

After a solver run leaves some flights unallocated, this module
produces a ranked list of operator-approvable override rows that
would unblock each flight on the *next* re-solve.

API.
    ``recommend_for_unallocated(flights, staff_today, matrix,
    assignments, elig_ctx, ops_day)`` returns a
    ``list[FlightRecommendations]`` — one entry per flight that the
    solver left unallocated. Each entry holds up to three
    ``OverrideSuggestion`` records sorted by intrusiveness (least
    invasive first).

Read-only.  This module reads engine outputs and produces
suggestions; the operator (via the chat layer in
``src.agent.tools``) decides which to apply. The apply path
writes override rows and triggers a re-solve.

Intrusiveness ranking (lowest = most preferred):

  1.  WaiveH10Pair       — one 5-14 min gap allowed on one staff.
                            Operational cost: small (one tight
                            sequence).
  2.  RaiseCapForFlight   — +1 cap relief for one staff.
                            Operational cost: moderate (one person
                            does 25 instead of 24 today).
  3.  SkipP2FBuffer       — waive P2F handler's D-3h buffer for one
                            flight. Operational cost: moderate
                            (handler's briefing-time buffer broken
                            for that flight).
  4.  SkipINTLRemoval     — keep both INTL DEP and its would-be-
                            removed preceding flight on the same
                            handler.  Operational cost: high
                            (handler does 2 back-to-back without
                            buffer relief).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_t

from ..schemas import FlightInput, StaffMember
from .caps import hard_cap_for
from .eligibility import (
    EligibilityContext,
    ExcludeReason,
    check as elig_check,
)
from .windows import (
    in_p2f_block,
    spacing_clear,
    spacing_key,
    std_to_ops_day_minutes,
)


@dataclass(frozen=True)
class OverrideSuggestion:
    """One proposed override row + its rationale + intrusiveness.

    ``override_type`` is one of: ``waive_h10_pair``, ``raise_cap``,
    ``skip_p2f_buffer``, ``skip_intl_removal``. ``payload`` is the
    dict the apply-tool will use to construct the override row.
    """

    intrusiveness: int          # lower = more preferred
    override_type: str
    rationale: str              # plain-English why this would work
    payload: dict               # override row values
    target_staff_name: str = "" # display only — empty for INTL removal


@dataclass(frozen=True)
class FlightRecommendations:
    """All viable override suggestions for one unallocated flight.

    ``suggestions`` is sorted ascending by ``intrusiveness``.
    Operators are expected to pick at most one per flight.
    """

    flight_id: str              # ``f"{flt}|{std.iso}"`` join key
    flt: str                    # bare flight number for display
    dep: str
    arr: str
    std_iso: str                # ``HH:MM`` string for display
    is_international: bool
    suggestions: list[OverrideSuggestion] = field(default_factory=list)


# ---------- intrusiveness weights ----------
_W_WAIVE_H10 = 10
_W_RAISE_CAP = 20
_W_SKIP_P2F_BUFFER = 30
_W_SKIP_INTL_REMOVAL = 40


# ---------- internals ----------

def _h10_conflicts(
    candidate_assignments: list[FlightInput],
    flight: FlightInput,
    ops_day: date_t,
) -> list[FlightInput]:
    """Every flight on a candidate staff's day-list whose STD is inside
    the H10 spacing floor of ``flight`` (15 min; 30 for domestic->INTL
    and for two P2F flights), earliest first.

    ALL of them, not just the first: waiving only one leaves the next
    neighbour still blocking, so the flight would stay unallocated no
    matter how often the suggestion is applied.
    """
    key = spacing_key(flight, ops_day)
    out = [
        other for other in candidate_assignments
        if other.unique_id != flight.unique_id
        and not spacing_clear(key, [spacing_key(other, ops_day)])
    ]
    out.sort(key=lambda f: spacing_key(f, ops_day)[0])
    return out


def _is_p2f_buffer_blocked(
    flight: FlightInput,
    staff: StaffMember,
    ctx: EligibilityContext,
) -> bool:
    """True iff F6's P2F buffer (STD-2hrs to STD+1hr) would reject this
    (flight, staff) pair.  Mirrors the check in ``eligibility.check`` F6
    so we can detect it for the recommender."""
    anchors = ctx.p2f_flight_minutes_by_handler.get(
        staff.employee_id, [],
    )
    if not anchors:
        return False
    target = std_to_ops_day_minutes(
        flight.std, flight.date, ctx.ops_day,
    )
    return in_p2f_block(target, anchors)


def _suggestions_for_flight(
    flight: FlightInput,
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    assignments: dict[str, str],
    elig_ctx: EligibilityContext,
    flights_per_staff: dict[str, list[FlightInput]],
    ops_day: date_t,
) -> list[OverrideSuggestion]:
    """Build the ranked suggestion list for one unallocated flight."""
    out: list[OverrideSuggestion] = []
    flight_std_iso = flight.std.isoformat(timespec="minutes")

    # Path A: the flight passed eligibility for some staff but the
    # solver chose to leave it unallocated. Inspect each such
    # candidate to find which hard constraint blocked them.
    eligible_ids = matrix.get(flight.unique_id, set())
    staff_by_id = {s.employee_id: s for s in staff_today}
    # 2026-05-22: only count flights that go against the cap. P2F
    # flights are exempt for the P2F handler. If we don't filter, the
    # recommender wrongly flags handlers as overflow candidates for
    # every unallocated domestic flight in their morning.
    from ..schemas import OpsClass as _OC
    def _cap_relevant_count(eid: str) -> int:
        n = 0
        for f in flights_per_staff.get(eid, []):
            if f.ops_class == _OC.P2F:
                continue   # P2F handler exemption (4-slot adjustment already removes domestic neighbours)
            n += 1
        return n
    for eid in sorted(eligible_ids):
        s = staff_by_id.get(eid)
        if s is None:
            continue
        cap = hard_cap_for(s)
        current_count = _cap_relevant_count(eid)
        # H10: candidate already has a flight within spacing-floor.
        conflicts = _h10_conflicts(
            flights_per_staff.get(eid, []), flight, ops_day,
        )
        if conflicts:
            conflict = conflicts[0]
            other_iso = conflict.std.isoformat(timespec="minutes")
            all_iso = ",".join(
                dict.fromkeys(c.std.isoformat(timespec="minutes") for c in conflicts)
            )
            out.append(OverrideSuggestion(
                intrusiveness=_W_WAIVE_H10,
                override_type="waive_h10_pair",
                target_staff_name=s.name,
                rationale=(
                    f"{s.name} (shift {s.shift_today}) is at "
                    f"{current_count}/{cap}; their flt {conflict.flt} "
                    f"at {other_iso} is the H10 blocker (gap "
                    f"{abs(std_to_ops_day_minutes(conflict.std, conflict.date, ops_day) - std_to_ops_day_minutes(flight.std, flight.date, ops_day))} min)."
                ),
                payload={
                    "type": "waive_h10_pair",
                    "flight": flight.flt,
                    "std": flight_std_iso,
                    "employee": s.name,
                    "other_std": other_iso,
                    # comma-separated; apply writes one waiver row each
                    "other_stds": all_iso,
                },
            ))
            continue
        # H16: candidate is at the cap (no H10 conflict).
        if current_count >= cap:
            out.append(OverrideSuggestion(
                intrusiveness=_W_RAISE_CAP,
                override_type="raise_cap",
                target_staff_name=s.name,
                rationale=(
                    f"{s.name} (shift {s.shift_today}) is at the H16 "
                    f"cap {current_count}/{cap}. Allowing this flight "
                    "to land 'on top' raises their effective cap by 1 "
                    "for this run only."
                ),
                payload={
                    "type": "raise_cap",
                    "flight": flight.flt,
                    "std": flight_std_iso,
                    "employee": s.name,
                },
            ))

    # Path B: every staff failed eligibility (zero matrix). Inspect
    # diagnose-style: find staff who pass everything EXCEPT F6 (P2F
    # buffer) and propose SkipP2FBuffer.
    if not eligible_ids:
        for s in staff_today:
            if s.shift_today is None:
                continue
            if not _is_p2f_buffer_blocked(flight, s, elig_ctx):
                continue
            # Verify this is the ONLY reason — re-run eligibility with
            # the buffer pretended to be skipped.
            elig_no_buffer = EligibilityContext(
                ops_day=elig_ctx.ops_day,
                p2f_handler_by_shift=elig_ctx.p2f_handler_by_shift,
                p2f_flight_minutes_by_handler={},  # blank → no buffer
            )
            reason = elig_check(flight, s, elig_no_buffer)
            if reason is None:
                out.append(OverrideSuggestion(
                    intrusiveness=_W_SKIP_P2F_BUFFER,
                    override_type="skip_p2f_buffer",
                    target_staff_name=s.name,
                    rationale=(
                        f"{s.name} (P2F handler) is blocked only by "
                        "their D-3hr buffer for this flight. Skipping "
                        "the buffer for this single flight would let "
                        "them take it."
                    ),
                    payload={
                        "type": "skip_p2f_buffer",
                        "flight": flight.flt,
                        "std": flight_std_iso,
                        "employee": s.name,
                    },
                ))

    # Path C (INTL only): SkipINTLRemoval doesn't unblock the INTL
    # flight itself (that's already allocated when it's INTL), but
    # if the flight got displaced into UNALLOCATED via a post-pass
    # removal, suggest the override. Detection: flight is plain
    # domestic AND there's an INTL DEP on the same handler within
    # the post-pass removal window.
    if not flight.is_international and not eligible_ids:
        # Check if any INTL DEP within ~3 hours after this flight could
        # be the post-pass remover.
        for assigned_uid, assigned_eid in assignments.items():
            if assigned_uid == flight.unique_id:
                continue
            other_flight = next(
                (f for f in flights_per_staff.get(assigned_eid, [])
                 if f.unique_id == assigned_uid),
                None,
            )
            if other_flight is None or not other_flight.is_international:
                continue
            f_min = std_to_ops_day_minutes(
                flight.std, flight.date, ops_day,
            )
            o_min = std_to_ops_day_minutes(
                other_flight.std, other_flight.date, ops_day,
            )
            if 0 <= o_min - f_min <= 180:    # within 3 hrs
                out.append(OverrideSuggestion(
                    intrusiveness=_W_SKIP_INTL_REMOVAL,
                    override_type="skip_intl_removal",
                    target_staff_name="",
                    rationale=(
                        f"INTL flt {other_flight.flt} at "
                        f"{other_flight.std.isoformat(timespec='minutes')} "
                        "displaced this flight via the post-pass "
                        "preceding-flight removal. Skipping that "
                        "removal keeps both flights on the same "
                        "handler."
                    ),
                    payload={
                        "type": "skip_intl_removal",
                        "flight": other_flight.flt,
                        "std": other_flight.std.isoformat(timespec="minutes"),
                    },
                ))
                break

    out.sort(key=lambda s: s.intrusiveness)
    return out[:3]   # keep top 3 per flight to avoid choice overload


# ---------- public entry ----------

def recommend_for_unallocated(
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    assignments: dict[str, str],
    elig_ctx: EligibilityContext,
    ops_day: date_t,
) -> list[FlightRecommendations]:
    """Produce ranked override suggestions for every unallocated flight.

    Returns a list ordered by flight STD ascending. Each entry has at
    most 3 suggestions, sorted by intrusiveness (least first).
    """
    # Group assigned flights per staff so candidate checks are O(1).
    flights_per_staff: dict[str, list[FlightInput]] = {}
    flight_by_uid = {f.unique_id: f for f in flights}
    for uid, eid in assignments.items():
        f = flight_by_uid.get(uid)
        if f is None:
            continue
        flights_per_staff.setdefault(eid, []).append(f)

    out: list[FlightRecommendations] = []
    for f in flights:
        if f.unique_id in assignments:
            continue
        suggestions = _suggestions_for_flight(
            f, staff_today, matrix, assignments,
            elig_ctx, flights_per_staff, ops_day,
        )
        out.append(FlightRecommendations(
            flight_id=f.unique_id,
            flt=f.flt,
            dep=f.dep,
            arr=f.arr,
            std_iso=f.std.isoformat(timespec="minutes"),
            is_international=f.is_international,
            suggestions=suggestions,
        ))
    out.sort(key=lambda r: r.std_iso)
    return out
