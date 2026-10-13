"""The clique form of H10 in the solver forbids exactly the pairs
``required_spacing_min`` forbids — no more, no fewer."""

import random
from datetime import date, time
from itertools import combinations

from src.allocator.windows import required_spacing_min, spacing_key
from src.schemas import FlightInput, OpsClass, Role, StaffMember
from src.solver.allocator_cpsat import solve_allocation

D = date(2026, 5, 1)


def _flight(i, minute, intl=False, p2f=False):
    return FlightInput(
        date=D, flt=f"6E{i}", type="J", ac="320",
        dep="SIN" if intl else "DEL", arr="BOM",
        std=time(minute // 60, minute % 60), load=100,
        ops_class=OpsClass.P2F if p2f else OpsClass.DAY,
        is_international=intl,
    )


def _both_allowed(fa, fb):
    """Can one person take both flights? Ask the solver directly: pin
    both to the same (only) staff member and see if it stays feasible."""
    staff = [StaffMember(employee_id="S1", name="S1", role=Role.STAFF,
                         date=D, shift_today="M")]
    elig = {fa.unique_id: {"S1"}, fb.unique_id: {"S1"}}
    res = solve_allocation(
        [fa, fb], staff, elig, ops_day=D, max_seconds=5,
    )
    return len(res.assignments) == 2


def test_clique_h10_matches_pairwise_rule():
    rng = random.Random(3)
    # Times in 5-minute steps around a busy hour, mixed types.
    flights = [
        _flight(
            i, 6 * 60 + 5 * rng.randint(0, 12),
            intl=rng.random() < 0.35, p2f=rng.random() < 0.25,
        )
        for i in range(12)
    ]
    for fa, fb in combinations(flights, 2):
        ka, kb = spacing_key(fa, D), spacing_key(fb, D)
        expected = abs(ka[0] - kb[0]) >= required_spacing_min(ka, kb)
        assert _both_allowed(fa, fb) == expected, (fa.std, fb.std, ka, kb)


def test_clique_h10_on_a_dense_schedule_never_underspaces():
    rng = random.Random(11)
    flights = [
        _flight(i, 5 * 60 + 5 * rng.randint(0, 40), intl=rng.random() < 0.3)
        for i in range(40)
    ]
    staff = [StaffMember(employee_id=f"S{k}", name=f"S{k}", role=Role.STAFF,
                         date=D, shift_today="M") for k in range(4)]
    elig = {f.unique_id: {s.employee_id for s in staff} for f in flights}
    res = solve_allocation(flights, staff, elig, ops_day=D, max_seconds=10)
    by_uid = {f.unique_id: f for f in flights}
    mine: dict[str, list] = {}
    for uid, eid in res.assignments.items():
        mine.setdefault(eid, []).append(spacing_key(by_uid[uid], D))
    for keys in mine.values():
        for ka, kb in combinations(keys, 2):
            assert abs(ka[0] - kb[0]) >= required_spacing_min(ka, kb)
