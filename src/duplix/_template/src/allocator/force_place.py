"""Last-resort placement: no flight is ever left unallocated.

The solver and post-passes treat spacing (H10), caps (H16), the P2F
nomination (H4) and shift-window eligibility as HARD rules, so on a
tight day a few flights come out with nobody to take them. Operator
direction (2026-10-01): every flight gets a person — if it takes a gap
below 15 min (30 for domestic->INTL), a cap overrun, or someone outside
the usual eligibility, do it, and say so loudly.

This runs after every other pass, on whatever is still unplaced, and
only ever ADDS assignments — it never moves a flight that already has
an owner, so it cannot disturb the rest of the plan.

Per leftover flight it picks the staff member who breaks the fewest /
mildest rules, in this order of importance:

  1. eligibility tier   fully eligible > on shift & in window (P2F: and
                        P2F-licensed M/A/N) > in window > anyone on shift
  2. cap overrun        under cap beats over cap
  3. spacing shortfall  the smallest gap violation (minutes short of
                        the 15 / 30 floor); a clear gap scores 0
  4. load               fewest flights so far, then employee id
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_t

from ..schemas import FlightInput, OpsClass, Role, StaffMember
from .caps import hard_cap_for, preferred_target_for
from .eligibility import EligibilityContext, check as elig_check
from .windows import (
    is_in_std_window_for,
    required_spacing_min,
    spacing_key,
    std_to_ops_day_minutes,
)

_TIER_LABEL = {
    0: "",
    1: "not normally eligible (e.g. not the nominated P2F handler)",
    2: "outside the P2F-licence/handler rules",
    3: "outside the shift's STD window",
}


@dataclass
class ForcedPlacement:
    flight: FlightInput
    employee_id: str
    staff_name: str
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "; ".join(self.notes) if self.notes else "no rule relaxed"


def _spacing_shortfall(
    flight: FlightInput, mine: list[tuple[FlightInput, tuple]],
    ops_day: date_t,
) -> tuple[int, str]:
    """(worst minutes short of the required gap, human text). 0 = clear."""
    key = spacing_key(flight, ops_day)
    worst = 0
    text = ""
    for other, okey in mine:
        gap = abs(key[0] - okey[0])
        need = required_spacing_min(key, okey)
        if gap < need and need - gap > worst:
            worst = need - gap
            text = (
                f"only {gap} min from flt {other.flt} "
                f"({other.std.isoformat(timespec='minutes')}), needs {need}"
            )
    return worst, text


def _tier(
    flight: FlightInput, s: StaffMember, ctx: EligibilityContext | None,
    ops_day: date_t,
) -> int:
    if ctx is not None and elig_check(flight, s, ctx) is None:
        return 0
    in_window = is_in_std_window_for(
        flight.std, flight.date, ops_day, s.shift_today, s.role,
    )
    if flight.ops_class == OpsClass.P2F:
        if in_window and s.is_p2f_licensed and s.shift_today in ("M", "A", "N"):
            return 1
    elif in_window:
        # Newbies stay off INTL even here — a safety rule, not a quota.
        if not (flight.is_international and s.is_newbie):
            return 1
    if in_window:
        return 2
    return 3


def force_place_remaining(
    leftovers: list[FlightInput],
    staff: list[StaffMember],
    assignments: dict[str, str],
    all_flights: list[FlightInput],
    *,
    ops_day: date_t,
    elig_ctx: EligibilityContext | None = None,
    max_std_by_staff: dict[str, int] | None = None,
    self_plan=None,
) -> list[ForcedPlacement]:
    """Place every flight in ``leftovers``; returns what was forced.

    ``assignments`` is the CURRENT ``{unique_id: employee_id}`` for all
    placed flights (used for each person's existing load and spacing).
    It is NOT modified. Raises nothing for lack of candidates: if no
    staff member is on shift at all there is nobody to place on and the
    flight is simply absent from the result.
    """
    flight_by_uid = {f.unique_id: f for f in all_flights}
    flight_by_uid.update({f.unique_id: f for f in leftovers})
    pool = [s for s in staff if s.role != Role.AM and s.shift_today is not None]
    if not pool:
        return []

    mine: dict[str, list[tuple[FlightInput, tuple]]] = defaultdict(list)
    count: dict[str, int] = defaultdict(int)
    for uid, eid in assignments.items():
        f = flight_by_uid.get(uid)
        if f is None:
            continue
        mine[eid].append((f, spacing_key(f, ops_day)))
        # P2F flights count toward the cap, exactly as in the solver's H16.
        count[eid] += 1
    caps = {s.employee_id: hard_cap_for(s) for s in pool}
    targets = {s.employee_id: preferred_target_for(s) for s in pool}

    # P2F first (they have the fewest options), then the hardest-to-place
    # INTL, then chronological.
    order = sorted(
        leftovers,
        key=lambda f: (
            f.ops_class != OpsClass.P2F,
            not f.is_international,
            std_to_ops_day_minutes(f.std, f.date, ops_day),
        ),
    )

    _max_std = max_std_by_staff or {}
    out: list[ForcedPlacement] = []
    for f in order:
        best = None
        f_minute = std_to_ops_day_minutes(f.std, f.date, ops_day)
        for s in pool:
            tier = _tier(f, s, elig_ctx, ops_day)
            # Released-early staff are the last resort for late flights.
            late = f_minute > _max_std.get(s.employee_id, 10**9)
            shortfall, why_space = _spacing_shortfall(
                f, mine[s.employee_id], ops_day,
            )
            cap = caps.get(s.employee_id, 0)
            over = max(0, count[s.employee_id] + 1 - cap)
            # Load is judged against the person's own target (ZC ~14,
            # N ~19, STAFF ~22), not the raw count — otherwise ZCs and
            # N staff always look "least loaded" next to day STAFF.
            load = count[s.employee_id] - targets[s.employee_id]
            # No planner for a flight before they arrive: relaxed only
            # when every other option breaks a rule too (2026-10-16).
            unplanned = bool(
                self_plan is not None
                and not self_plan.ok(
                    s.employee_id, f.unique_id,
                    [f2.unique_id for f2, _ in mine[s.employee_id]],
                )
            )
            rank = (tier, late, over, shortfall, unplanned, load, s.employee_id)
            if best is None or rank < best[0]:
                best = (rank, s, tier, over, shortfall, why_space, cap, unplanned)
        assert best is not None
        _, s, tier, over, shortfall, why_space, cap, unplanned = best

        notes: list[str] = []
        if tier:
            notes.append(_TIER_LABEL[tier])
        if shortfall:
            notes.append(f"spacing relaxed: {why_space}")
        if over:
            notes.append(
                f"over cap: {count[s.employee_id] + 1}/{cap} flights"
            )
        if unplanned:
            notes.append(
                "no planner: departs before "
                f"{self_plan.latest_self_planned(s.employee_id)}, "
                "planned before their shift starts"
            )
        out.append(ForcedPlacement(f, s.employee_id, s.name, notes))

        key = spacing_key(f, ops_day)
        mine[s.employee_id].append((f, key))
        count[s.employee_id] += 1
    return out
