"""Behaviour of the independent H1 / H10 / H16 checker."""

from datetime import date, time
from types import SimpleNamespace

from src.allocator.invariants import check_allocation, flight_key

D = date(2026, 5, 1)


def row(flt, h, m, emp, dep="DEL", arr="BOM", day=D):
    return SimpleNamespace(
        flt=flt, dep=dep, arr=arr, std=time(h, m), date=day, staff_employee_id=emp
    )


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


def test_h10_flags_two_flights_under_15_min_outside_bands():
    rows = [row("6E1", 9, 0, "E1"), row("6E2", 9, 14, "E1")]
    assert rules(check_allocation(rows, ops_day=D)) == ["H10"]


def test_h10_allows_exactly_15_min_outside_bands():
    rows = [row("6E1", 9, 0, "E1"), row("6E2", 9, 15, "E1")]
    assert check_allocation(rows, ops_day=D) == []


def test_h10_relaxed_band_allows_10_min_only_when_both_in_band():
    ok = [row("6E1", 20, 30, "E1"), row("6E2", 20, 40, "E1")]
    assert check_allocation(ok, ops_day=D) == []
    too_close = [row("6E1", 20, 30, "E1"), row("6E2", 20, 39, "E1")]
    assert rules(check_allocation(too_close, ops_day=D)) == ["H10"]
    one_out = [row("6E1", 22, 0, "E1"), row("6E2", 22, 10, "E1")]  # 22:10 is outside
    assert rules(check_allocation(one_out, ops_day=D)) == ["H10"]


def test_h10_finds_non_adjacent_pair_violations():
    # a-b fine (10 min, in band), b-c only 4 min: caught; nothing hides behind a neighbour
    rows = [row("A", 20, 0, "E1"), row("B", 20, 10, "E1"), row("C", 20, 14, "E1")]
    assert rules(check_allocation(rows, ops_day=D)) == ["H10"]


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
