"""Greedy fallback allocator — Step 4 safety net.

CP-SAT (``solver/allocator_cpsat.py``) is the primary allocator: it
enforces every hard rule (H1/H10/H16/H4/H17/H18...) simultaneously and,
when it reaches OPTIMAL, provably gives the best weighted result. But
it is an EXACT solver — if any combination of hard constraints turns
out to be mutually unsatisfiable (a mis-tuned config value, a day with
an unusual shape, a bug not yet caught), it reports INFEASIBLE and
hands back ZERO assignments for the WHOLE day, even for the 99% of
flights that had nothing to do with the conflict. That is what
happened in production on 2026-09-22 (a static N/ZC workload floor of
14 with zero actual night-ops flights).

This module is the fallback for exactly that situation. It is invoked
ONLY when CP-SAT returns a non-feasible status. It ignores every soft
objective (workload balance, handover preference, INTL spacing
fairness, day-level targeting...) — it exists purely so the operator
gets a usable, physically-valid allocation instead of a wall of
unallocated flights, with a loud warning that manual review is needed.

Hard rules this DOES still enforce (the ones that are non-negotiable
even in a degraded mode):
  H1   one staff per flight             — never double-books a flight
  H10  same-staff spacing               — never overlaps/underspaces
       one person's flights
  H16  per-staff hard cap               — never exceeds a staff's max

Hard rules this does NOT enforce (left to the operator to review via
the Warnings / Unallocated tabs when a greedy-fallback run happens):
  H17 P2F-per-handler cap of 8, H18
  workload-spread bucketing, INTL D-75 coverage nuances.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date as date_t

from ..schemas import FlightInput, OpsClass, StaffMember
from .caps import hard_cap_for, preferred_target_for
from .windows import SpacingKey, spacing_clear, spacing_key, std_to_ops_day_minutes


#: How far ahead the greedy looks when deciding who is least needed next.
LOOKAHEAD_MIN = 60
#: How much a person's load (as a fraction of their target) counts
#: against how much they are needed next — see ``greedy_allocate``.
LOAD_WEIGHT = 200


def greedy_allocate(
    flights: list[FlightInput],
    staff: list[StaffMember],
    eligibility: dict[str, set[str]],
    *,
    ops_day: date_t,
    preassigned: dict[str, str] | None = None,
) -> dict[str, str]:
    """Best-effort assignment respecting H1, H10, H16 only.

    Places every P2F flight first (so the nominated handler is never
    crowded out by normal flights), then all other flights, each group
    in chronological (STD) order — and, at the same STD, the flight with
    the fewest eligible staff first. Each flight goes to the eligible
    staff member (spacing and hard cap clear) with the lowest score

        (other flights they could take in the next ``LOOKAHEAD_MIN``)
        + ``LOAD_WEIGHT`` x (flights so far / their own band target)

    The first part keeps flexible people free for the departures right
    behind this one, which is what makes the peaks fit; the second keeps
    loads in step across roles (a ZC at 9/14 is "emptier" than STAFF at
    18/22). On a 2,100-flight test day this left 79 flights unplaced
    against 98 for plain least-loaded (and 88 after 40s of CP-SAT), with
    every role and shift near its natural share.

    Returns ``{flight.unique_id: employee_id}`` for whatever it
    managed to place. A flight with no eligible-and-available staff is
    simply absent from the result, exactly like a partial CP-SAT
    solve — the caller's existing UNALLOCATED bookkeeping picks it up
    the same way either way.
    """
    staff_by_id = {s.employee_id: s for s in staff}
    caps = {s.employee_id: hard_cap_for(s) for s in staff}
    counts: dict[str, int] = defaultdict(int)
    # Per-staff SpacingKey list for H10 spacing checks.
    assigned_by_staff: dict[str, list[SpacingKey]] = defaultdict(list)

    # P2F FIRST (2026-09-22, user direction): the nominated handler's P2F
    # flights are placed before any normal flight is considered.
    # Walking purely by time let a handler fill up on earlier normal
    # flights and then hit his cap / spacing when his P2F flights came
    # up. Within each group the order is still chronological.
    ordered = sorted(
        flights,
        key=lambda f: (
            f.ops_class != OpsClass.P2F,
            std_to_ops_day_minutes(f.std, f.date, ops_day),
            len(eligibility.get(f.unique_id, ())),
        ),
    )
    targets = {s.employee_id: preferred_target_for(s) for s in staff}
    # Each staff member's eligible STDs, sorted — for the lookahead.
    eligible_times: dict[str, list[int]] = defaultdict(list)
    for f in flights:
        t = std_to_ops_day_minutes(f.std, f.date, ops_day)
        for eid in eligibility.get(f.unique_id, ()):
            eligible_times[eid].append(t)
    for times in eligible_times.values():
        times.sort()

    def _need_soon(eid: str, t: int) -> int:
        times = eligible_times[eid]
        return bisect_right(times, t + LOOKAHEAD_MIN) - bisect_right(times, t)

    assignments: dict[str, str] = {}
    # ``preassigned`` ({unique_id: employee_id}) are flights already fixed
    # elsewhere — re-solve pins, or flights the caller holds outside
    # ``flights``. They are kept as-is (when still eligible) and occupy
    # their owner's cap and spacing before anything else is placed, so a
    # fallback run never reshuffles a pinned day.
    if preassigned:
        flight_by_uid = {f.unique_id: f for f in flights}
        for uid, eid in preassigned.items():
            if eid not in staff_by_id:
                continue
            f = flight_by_uid.get(uid)
            if f is not None and eid not in eligibility.get(uid, ()):
                continue
            if f is not None:
                assignments[uid] = eid
                counts[eid] += 1
                assigned_by_staff[eid].append(spacing_key(f, ops_day))
    for f in ordered:
        if f.unique_id in assignments:
            continue
        key = spacing_key(f, ops_day)
        candidates: list[str] = []
        for eid in eligibility.get(f.unique_id, ()):
            if staff_by_id.get(eid) is None:
                continue
            cap = caps.get(eid, 0)
            if cap <= 0 or counts[eid] >= cap:
                continue
            if spacing_clear(key, assigned_by_staff[eid]):
                candidates.append(eid)
        if not candidates:
            continue
        pick = min(
            candidates,
            key=lambda eid: (
                _need_soon(eid, key[0])
                + LOAD_WEIGHT * counts[eid] / max(1, targets[eid]),
                eid,
            ),
        )
        assignments[f.unique_id] = pick
        counts[pick] += 1
        assigned_by_staff[pick].append(key)

    return assignments
