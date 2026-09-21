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
       one person's flights (NORSE-NORSE pairs are exempt, same as
       the main solver, since a NORSE handler doesn't physically
       operate the flight)
  H16  per-staff hard cap               — never exceeds a staff's max

Hard rules this does NOT enforce (left to the operator to review via
the Warnings / Unallocated tabs when a greedy-fallback run happens):
  H4 P2F partial-block windows, H17 P2F-per-handler cap of 8, H18
  workload-spread bucketing, INTL D-75 coverage nuances.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_t

from ..schemas import FlightInput, OpsClass, StaffMember
from .caps import hard_cap_for
from .windows import required_spacing_min, std_to_ops_day_minutes


def greedy_allocate(
    flights: list[FlightInput],
    staff: list[StaffMember],
    eligibility: dict[str, set[str]],
    *,
    ops_day: date_t,
) -> dict[str, str]:
    """Best-effort assignment respecting H1, H10, H16 only.

    Walks flights in chronological (STD) order; for each, picks the
    least-loaded eligible staff who doesn't violate that staff's
    spacing or hard cap. No optimality guarantee and no workload
    balancing beyond "prefer whoever has fewer flights so far" — this
    is a safety net, not a replacement for the CP-SAT model.

    Returns ``{flight.unique_id: employee_id}`` for whatever it
    managed to place. A flight with no eligible-and-available staff is
    simply absent from the result, exactly like a partial CP-SAT
    solve — the caller's existing UNALLOCATED bookkeeping picks it up
    the same way either way.
    """
    staff_by_id = {s.employee_id: s for s in staff}
    caps = {s.employee_id: hard_cap_for(s) for s in staff}
    counts: dict[str, int] = defaultdict(int)
    # Per-staff list of (std_minutes, is_norse) for H10 spacing checks.
    assigned_by_staff: dict[str, list[tuple[int, bool]]] = defaultdict(list)

    ordered = sorted(
        flights,
        key=lambda f: std_to_ops_day_minutes(f.std, f.date, ops_day),
    )

    assignments: dict[str, str] = {}
    for f in ordered:
        std_min = std_to_ops_day_minutes(f.std, f.date, ops_day)
        is_norse = f.ops_class == OpsClass.NORSE
        candidates: list[str] = []
        for eid in eligibility.get(f.unique_id, ()):
            if staff_by_id.get(eid) is None:
                continue
            cap = caps.get(eid, 0)
            if cap <= 0 or counts[eid] >= cap:
                continue
            ok = True
            if not is_norse:
                for other_std, other_norse in assigned_by_staff[eid]:
                    if other_norse:
                        continue  # NORSE-NORSE pairs skip H10 spacing
                    lo, hi = min(std_min, other_std), max(std_min, other_std)
                    if hi - lo < required_spacing_min(lo, hi):
                        ok = False
                        break
            if ok:
                candidates.append(eid)
        if not candidates:
            continue
        candidates.sort(key=lambda eid: (counts[eid], eid))
        pick = candidates[0]
        assignments[f.unique_id] = pick
        counts[pick] += 1
        assigned_by_staff[pick].append((std_min, is_norse))

    return assignments
