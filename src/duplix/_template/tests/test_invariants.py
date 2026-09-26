"""Behaviour of the independent H1 / H10 / H16 checker."""

from datetime import date, time
from types import SimpleNamespace

from src.allocator.invariants import check_allocation, flight_key

D = date(2026, 5, 1)


def row(flt, h, m, emp, dep="DEL", arr="BOM", day=D, intl=None, sheet=None):
    r = SimpleNamespace(
        flt=flt, dep=dep, arr=arr, std=time(h, m), date=day, staff_employee_id=emp
    )
    # Rows without these attributes read as a plain domestic flight.
    if intl is not None:
        r.is_international = intl
    if sheet is not None:
        r.sheet_target = sheet
    return r


def rules(violations):
    return sorted(v.rule for v in violations)


def test_clean_allocation_has_no_violations():
    rows = [row("6E1", 9, 0, "E1"), row("6E2", 9, 30, "E1"), row("6E3", 9, 0, "E2")]
    assert check_allocation(rows, ops_day=D, caps={"E1": 5, "E2": 5}) == []


def test_unassigned_rows_are_ignored():
    rows = [row("6E1", 9, 0, ""), row("6E2", 9, 5, "")]
    assert check_allocation(rows, ops_day=D) == []


def test_h1_flags_a_flight_owned_twice():
    rows = [row("6E1", 9, 0, "E1"), row("6E1", 9, 0, "E2")]
    assert rules(check_allocation(rows, ops_day=D)) == ["H1"]


def test_h10_flags_two_flights_under_15_min():
    rows = [row("6E1", 9, 0, "E1"), row("6E2", 9, 14, "E1")]
    assert rules(check_allocation(rows, ops_day=D)) == ["H10"]


def test_h10_allows_exactly_15_min():
    rows = [row("6E1", 9, 0, "E1"), row("6E2", 9, 15, "E1")]
    assert check_allocation(rows, ops_day=D) == []


def test_h10_rush_band_no_longer_allows_10_min():
    rows = [row("6E1", 20, 30, "E1"), row("6E2", 20, 40, "E1")]
    assert rules(check_allocation(rows, ops_day=D)) == ["H10"]


def test_h10_domestic_then_intl_needs_30_min():
    dom = row("6E1", 9, 0, "E1", intl=False)
    assert rules(check_allocation([dom, row("6E2", 9, 29, "E1", intl=True)], ops_day=D)) == ["H10"]
    assert check_allocation([dom, row("6E2", 9, 30, "E1", intl=True)], ops_day=D) == []


def test_h10_intl_then_domestic_and_intl_pairs_need_15_min():
    a = row("6E1", 9, 0, "E1", intl=True)
    assert check_allocation([a, row("6E2", 9, 15, "E1", intl=False)], ops_day=D) == []
    assert check_allocation([a, row("6E2", 9, 15, "E1", intl=True)], ops_day=D) == []


def test_h10_two_p2f_flights_need_30_min():
    a = row("P1", 9, 0, "E1", sheet="P2F")
    assert rules(check_allocation([a, row("P2", 9, 29, "E1", sheet="P2F")], ops_day=D)) == ["H10"]
    assert check_allocation([a, row("P2", 9, 30, "E1", sheet="P2F")], ops_day=D) == []
    assert check_allocation([a, row("N1", 9, 15, "E1", sheet="DayOps")], ops_day=D) == []


def test_h10_checks_every_pair_not_just_neighbours():
    # B-C is 10 min (under 15) and A-C is a domestic->INTL pair 25 min apart.
    rows = [row("A", 9, 0, "E1"), row("B", 9, 15, "E1"), row("C", 9, 25, "E1", intl=True)]
    assert rules(check_allocation(rows, ops_day=D)) == ["H10", "H10"]


def test_h10_different_staff_never_conflict():
    rows = [row("6E1", 9, 0, "E1"), row("6E2", 9, 1, "E2")]
    assert check_allocation(rows, ops_day=D) == []


def test_h10_waived_pair_is_not_reported():
    a, b = row("6E1", 9, 0, "E1"), row("6E2", 9, 5, "E1")
    waived = {frozenset({flight_key(a), flight_key(b)})}
    assert check_allocation([a, b], ops_day=D, waived_pairs=waived) == []
    assert rules(check_allocation([a, b], ops_day=D)) == ["H10"]


def test_h10_uses_ops_day_minutes_across_midnight():
    d1 = date(2026, 5, 2)
    rows = [row("N1", 23, 55, "E1"), row("N2", 0, 5, "E1", day=d1)]  # 10 min apart
    assert rules(check_allocation(rows, ops_day=D)) == ["H10"]


def test_h16_cap_exceeded():
    rows = [row(f"F{i}", 6 + i, 0, "E1") for i in range(4)]
    v = check_allocation(rows, ops_day=D, caps={"E1": 3})
    assert rules(v) == ["H16"]
    assert v[0].employee_id == "E1"


def test_h16_skipped_without_caps_or_for_unknown_staff():
    rows = [row(f"F{i}", 6 + i, 0, "E1") for i in range(4)]
    assert check_allocation(rows, ops_day=D) == []
    assert check_allocation(rows, ops_day=D, caps={"E9": 1}) == []
