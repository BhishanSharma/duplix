"""Flights nobody can plan: keep them off people who'd start unplanned.

A flight is planned 75 minutes before departure (D-75). For a person
whose shift starts at 12:30, any flight before 13:45 has to be planned
before they arrive — by their handover partner from the previous shift
(``pairings.py``), who covers their first ``preplan_count`` flights.

2026-10-16 (user direction): when there is no planner for such a flight
— the person has no partner at all (e.g. the whole previous shift is
missing), or the flight is beyond what the partner covers — the person
must not hold it: their flights then start late enough (shift start +
75 min) for them to plan them themselves.

This module only answers "which flights break that" and "may this
person take this flight"; Allocate moves the excess through the repair
pass, and force-place relaxes it (flagged) only as a last resort.

Before that, M1 helps A (2026-10-16, user direction): an A person
holding more early flights than their M planner covers gets an M1
person — mid-shift at A's planning time, and not on their break then —
to plan the extra ones
(``backup_planners``, at most ``M1_HELP_MAX`` A flights per M1 person).

M is not covered: M's planners are the previous night's N shift, which
this run does not see. INTL flights (planned by the same handler start
to finish) and P2F flights (their own host annotations) are exempt.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date as date_t

from ..schemas import (
    FlightInput, OpsBoundary, OpsClass, Pair, PairRole, Role, StaffMember,
)
from .windows import hours_for, std_to_ops_day_minutes

PLAN_AHEAD_MIN = 75
# Shifts whose planners are generated today (see pairings._PLANNING_BOUNDARIES).
PLANNED_SHIFTS = ("M1", "A", "A1", "N")
# Most A flights one M1 person plans on top of their own work.
M1_HELP_MAX = 2


class SelfPlanRule:
    """Per-person cap on flights that must be planned before they arrive."""

    def __init__(
        self, pairs: list[Pair], staff_today: list[StaffMember],
        flights: list[FlightInput], ops_day: date_t,
    ) -> None:
        self.capacity: Counter[str] = Counter()
        for p in pairs:
            if p.preplan_count > 0 and p.next_employee_id:
                self.capacity[p.next_employee_id] += p.preplan_count
        # Each person's own start (a ZC's may differ from the shift's).
        self._shift = {
            s.employee_id: s.shift_today for s in staff_today
            if s.shift_today in PLANNED_SHIFTS
        }
        self._start = {
            s.employee_id: hours_for(s.shift_today, s.role)[0]
            for s in staff_today if s.shift_today in PLANNED_SHIFTS
        }
        self._minute = {
            f.unique_id: std_to_ops_day_minutes(f.std, f.date, ops_day)
            for f in flights
        }
        self._exempt = {
            f.unique_id for f in flights
            if f.is_international or f.ops_class == OpsClass.P2F
        }

    def needs_planner(self, eid: str, uid: str) -> bool:
        """True when ``eid`` holding ``uid`` needs someone to plan it
        before their shift starts."""
        shift = self._shift.get(eid)
        if shift is None or uid in self._exempt or uid not in self._minute:
            return False
        return self._minute[uid] - PLAN_AHEAD_MIN < self._start[eid]

    def latest_self_planned(self, eid: str) -> str:
        if eid not in self._start:
            return ""
        m = self._start[eid] + PLAN_AHEAD_MIN
        return f"{(m // 60) % 24:02d}:{m % 60:02d}"

    def ok(self, eid: str, uid: str, held: "set[str] | list[str]") -> bool:
        """May ``eid`` (already holding ``held``) also take ``uid``?"""
        if not self.needs_planner(eid, uid):
            return True
        early = sum(1 for h in held if h != uid and self.needs_planner(eid, h))
        return early < self.capacity[eid]

    def excess(self, assign: dict[str, str]) -> list[str]:
        """Flights held beyond what their holder's planner covers — the
        latest early ones go, the first ``capacity`` stay."""
        early: dict[str, list[str]] = defaultdict(list)
        for uid, eid in assign.items():
            if eid and self.needs_planner(eid, uid):
                early[eid].append(uid)
        out: list[str] = []
        for eid, uids in early.items():
            uids.sort(key=lambda u: self._minute[u])
            out.extend(uids[self.capacity[eid]:])
        return out


def backup_planners(
    pairs: list[Pair], staff_today: list[StaffMember],
    flights: list[FlightInput], ops_day: date_t, assign: dict[str, str],
    *, avoid_ids: "set[str] | frozenset[str]" = frozenset(),
    breaks: "dict[str, tuple[int, int]] | None" = None,
) -> list[Pair]:
    """``M1->A`` pairs for the A flights (held in ``assign``) that need
    planning before their holder arrives but are beyond what their M
    planner covers. Each such flight gets an M1 person on shift and not
    on their break (``breaks``: ops-day minutes, from the break pass) at
    its D-75, who plans fewer than ``M1_HELP_MAX`` A flights so far: fewest
    first, then the same role, then P2F handlers last, then roster order.
    Flights no M1 person can take stay with the self-plan rule."""
    rule = SelfPlanRule(pairs, staff_today, flights, ops_day)
    helpers = [s for s in staff_today if s.shift_today == "M1" and s.role in (Role.STAFF, Role.ZC)]
    if not helpers:
        return []
    by_id = {s.employee_id: s for s in staff_today}
    breaks = breaks or {}
    order = {s.employee_id: i for i, s in enumerate(helpers)}
    load: Counter[str] = Counter()
    for p in pairs:
        if p.boundary == OpsBoundary.M1_TO_A and p.prev_employee_id:
            load[p.prev_employee_id] += p.preplan_count
    extra = [
        u for u in rule.excess(assign)
        if rule._shift.get(assign[u]) == "A"
    ]
    extra.sort(key=lambda u: (rule._minute[u], assign[u]))
    count: Counter[tuple[str, str]] = Counter()
    for uid in extra:
        plan_at = rule._minute[uid] - PLAN_AHEAD_MIN
        holder = by_id[assign[uid]]
        free = [
            h for h in helpers
            if load[h.employee_id] < M1_HELP_MAX
            and hours_for("M1", h.role)[0] <= plan_at < hours_for("M1", h.role)[1]
            and not (breaks.get(h.employee_id, (0, 0))[0] <= plan_at
                     < breaks.get(h.employee_id, (0, 0))[1])
        ]
        if not free:
            continue
        pick = min(free, key=lambda h: (
            load[h.employee_id], h.role != holder.role,
            h.employee_id in avoid_ids, order[h.employee_id],
        ))
        load[pick.employee_id] += 1
        count[(pick.employee_id, holder.employee_id)] += 1
    return [
        Pair(
            boundary=OpsBoundary.M1_TO_A, pair_role=PairRole.PRIMARY,
            preplan_count=n,
            prev_employee_id=m1, prev_name=by_id[m1].name, prev_shift="M1",
            next_employee_id=a, next_name=by_id[a].name, next_shift="A",
        )
        for (m1, a), n in count.items()
    ]
