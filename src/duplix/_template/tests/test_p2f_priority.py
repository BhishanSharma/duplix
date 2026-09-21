"""P2F-first priority: the nominated handler's P2F flights come before
any normal flight (solver reservation + greedy fallback)."""

from datetime import date, time
from types import SimpleNamespace

from src.allocator.greedy_fallback import greedy_allocate
from src.allocator.p2f_priority import select_p2f_priority
from src.schemas import OpsClass, Role

D = date(2026, 5, 1)
H = "H1"  # the nominated P2F handler


def flight(uid, h, m, ops_class=OpsClass.P2F):
    return SimpleNamespace(
        unique_id=uid, date=D, std=time(h, m), ops_class=ops_class
    )


def select(flights, eligibility, **kw):
    kw.setdefault("cap_by_staff", {H: 24, "H2": 24})
    return select_p2f_priority(
        flights, eligibility, kw.pop("handlers", {H}), ops_day=D, **kw
    )


# ---------- select_p2f_priority ----------

def test_reserves_p2f_for_handler_and_ignores_normal_flights():
    fl = [flight("P1", 9, 0), flight("N1", 9, 0, OpsClass.DAY)]
    elig = {"P1": {H}, "N1": {H, "O1"}}
    reserved, skipped = select(fl, elig)
    assert reserved == {"P1": H}
    assert skipped == []


def test_p2f_flights_closer_than_h10_are_not_both_reserved():
    fl = [flight("P1", 9, 0), flight("P2", 9, 10)]  # 10 min apart, outside band
    reserved, skipped = select(fl, {"P1": {H}, "P2": {H}})
    assert reserved == {"P1": H}
    assert [u for u, _ in skipped] == ["P2"]


def test_waived_h10_pair_can_both_be_reserved():
    fl = [flight("P1", 9, 0), flight("P2", 9, 10)]
    reserved, skipped = select(
        fl, {"P1": {H}, "P2": {H}}, waive_h10_triples={("P1", "P2", H)}
    )
    assert set(reserved) == {"P1", "P2"}
    assert skipped == []


def test_h17_limit_of_eight_per_handler():
    fl = [flight(f"P{i}", 6 + i, 0) for i in range(10)]  # hourly, no clashes
    reserved, skipped = select(fl, {f.unique_id: {H} for f in fl})
    assert len(reserved) == 8
    assert len(skipped) == 2


def test_handler_cap_limits_reservations():
    fl = [flight(f"P{i}", 6 + i, 0) for i in range(5)]
    reserved, _ = select(
        fl, {f.unique_id: {H} for f in fl}, cap_by_staff={H: 3}
    )
    assert len(reserved) == 3


def test_flight_with_two_eligible_handlers_is_left_to_the_solver():
    fl = [flight("P1", 9, 0)]
    reserved, skipped = select(
        fl, {"P1": {H, "H2"}}, handlers={H, "H2"}
    )
    assert reserved == {}
    assert skipped[0][0] == "P1"


def test_flight_with_no_nominated_handler_is_skipped():
    reserved, skipped = select([flight("P1", 9, 0)], {"P1": {"O1"}})
    assert reserved == {}
    assert skipped[0][0] == "P1"


def test_skipped_when_a_normal_flight_is_already_pinned_too_close():
    fl = [flight("P1", 9, 0), flight("N1", 9, 5, OpsClass.DAY)]
    reserved, skipped = select(
        fl, {"P1": {H}, "N1": {H}}, pinned_assignments={"N1": H}
    )
    assert reserved == {}
    assert skipped[0][0] == "P1"


def test_p2f_pinned_to_the_handler_is_kept():
    fl = [flight("P1", 9, 0)]
    reserved, _ = select(fl, {"P1": {H}}, pinned_assignments={"P1": H})
    assert reserved == {"P1": H}


# ---------- greedy fallback ----------

def staff(emp, cap):
    return SimpleNamespace(
        employee_id=emp, role=Role.STAFF, shift_today="M", max_flights_cap=cap
    )


def test_greedy_gives_the_handler_his_p2f_before_normal_flights():
    """Handler (cap 3) is eligible for three early normal flights and one
    later P2F, and nobody else can take the normals. Chronological greedy
    filled him with the normals and left the P2F unallocated; P2F-first
    keeps the P2F and gives up a normal flight instead."""
    normals = [
        flight(f"N{i}", 6 + i, 0, OpsClass.DAY) for i in range(3)
    ]
    p2f = flight("P1", 12, 0)
    elig = {"P1": {H}}
    elig.update({f.unique_id: {H} for f in normals})
    result = greedy_allocate(
        normals + [p2f], [staff(H, 3)], elig, ops_day=D
    )
    assert result["P1"] == H
    assert len(result) == 3  # P2F + two of the three normals (cap 3)


def test_greedy_still_places_normal_flights_others_can_take():
    normals = [
        flight(f"N{i}", 6 + i, 0, OpsClass.DAY) for i in range(3)
    ]
    elig = {"P1": {H}}
    elig.update({f.unique_id: {H, "O1"} for f in normals})
    result = greedy_allocate(
        normals + [flight("P1", 12, 0)],
        [staff(H, 3), staff("O1", 10)],
        elig,
        ops_day=D,
    )
    assert result["P1"] == H
    assert set(result) == {"P1", "N0", "N1", "N2"}
