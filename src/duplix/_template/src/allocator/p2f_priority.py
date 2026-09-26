"""P2F-first priority: reserve each handler's P2F flights before anything else.

Why this exists
---------------
A P2F flight can only go to the shift's nominated handler (eligibility F4).
That same handler is also eligible for ordinary flights, and every one of
those competes with his P2F flights for the same cap (H16) and the same
15-minute spacing (H10). With every flight costing the same when left
unallocated, nothing told the allocator that the handler's P2F work comes
first, so he could end up filled with normal flights and a P2F left over.

``select_p2f_priority`` decides, per handler, which P2F flights are
*reserved* for him. The CP-SAT model then fixes those assignments and the
normal flights are solved around them; the greedy fallback places them
before it looks at any normal flight.

Only flights that can be reserved *safely* are returned, so fixing them
can never make the model infeasible:

* the flight must have exactly one nominated handler eligible for it
  (if two handlers could take it, the choice is left to the solver);
* at most ``per_handler_limit`` (H17: 8) per handler, and never more than
  the handler's own hard cap (H16);
* two reserved P2F flights for one handler must be at least 30 min
  apart (H10), unless the operator waived that pair;
* a flight is skipped when a normal flight already pinned to the handler
  (re-solve mode) is too close to it, or when it is pinned elsewhere.

Anything skipped is returned with a reason so the caller can log it. Those
flights are still allocated by the solver, just without the reservation.

The module only needs flights with ``unique_id``, ``date``, ``std``,
``ops_class`` and ``is_international`` — no solver dependency — so it is
cheap to unit test.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from datetime import date as date_t

from ..schemas import OpsClass
from .windows import SpacingKey, spacing_clear, spacing_key

# H17: no handler is asked to do more than this many P2F flights a day.
P2F_PER_HANDLER_LIMIT = 8


def select_p2f_priority(
    flights: Iterable,
    eligibility: Mapping[str, Collection[str]],
    handler_ids: Collection[str],
    *,
    ops_day: date_t,
    cap_by_staff: Mapping[str, int],
    per_handler_limit: int = P2F_PER_HANDLER_LIMIT,
    pinned_assignments: Mapping[str, str] | None = None,
    waive_h10_triples: Collection[tuple[str, str, str]] = frozenset(),
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Pick the P2F flights to reserve for their handlers.

    Returns ``(reserved, skipped)``:

    * ``reserved`` — ``{flight_unique_id: handler_employee_id}``
    * ``skipped``  — ``[(flight_unique_id, reason), ...]`` for every P2F
      flight that was *not* reserved.
    """
    pinned = dict(pinned_assignments or {})
    handler_set = set(handler_ids)
    flight_list = list(flights)
    key_of = {f.unique_id: spacing_key(f, ops_day) for f in flight_list}

    reserved: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []

    # Group each P2F flight under the single handler eligible for it.
    by_handler: dict[str, list] = defaultdict(list)
    for f in flight_list:
        if f.ops_class != OpsClass.P2F:
            continue
        candidates = set(eligibility.get(f.unique_id, ())) & handler_set
        if not candidates:
            skipped.append((f.unique_id, "no nominated handler is eligible"))
        elif len(candidates) > 1:
            skipped.append((f.unique_id, "more than one handler is eligible"))
        else:
            by_handler[next(iter(candidates))].append(f)

    # Times of ordinary flights already pinned to each handler (re-solve
    # mode). A reserved P2F flight must keep H10 spacing from these.
    p2f_uids = {f.unique_id for fl in by_handler.values() for f in fl}
    pinned_normal: dict[str, list[SpacingKey]] = defaultdict(list)
    for uid, eid in pinned.items():
        if eid in by_handler and uid in key_of and uid not in p2f_uids:
            pinned_normal[eid].append(key_of[uid])

    def _waived(uid_a: str, uid_b: str, emp: str) -> bool:
        return (
            (uid_a, uid_b, emp) in waive_h10_triples
            or (uid_b, uid_a, emp) in waive_h10_triples
        )

    for handler, p2f_flights in by_handler.items():
        limit = min(per_handler_limit, cap_by_staff.get(handler, 0))
        # Flights already pinned to this handler go first (they are fixed
        # anyway), then earliest STD.
        ordered = sorted(
            p2f_flights,
            key=lambda f: (pinned.get(f.unique_id) != handler, key_of[f.unique_id][0]),
        )
        taken: list = []
        for f in ordered:
            uid = f.unique_id
            pinned_to = pinned.get(uid)
            if pinned_to is not None and pinned_to != handler:
                skipped.append((uid, f"pinned to {pinned_to}, not the handler"))
                continue
            if len(taken) >= limit:
                skipped.append((uid, f"handler already has {limit} reserved (H17/H16)"))
                continue
            here = key_of[uid]
            clash = next(
                (
                    o for o in taken
                    if not _waived(uid, o.unique_id, handler)
                    and not spacing_clear(here, [key_of[o.unique_id]])
                ),
                None,
            )
            if clash is not None:
                skipped.append(
                    (uid, f"too close to reserved P2F {clash.unique_id} (H10)")
                )
                continue
            if not spacing_clear(here, pinned_normal.get(handler, ())):
                skipped.append((uid, "too close to a flight already pinned to the handler"))
                continue
            taken.append(f)
            reserved[uid] = handler

    return reserved, skipped
