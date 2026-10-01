"""Relievers for everyone but N, one reliever per person, early release
for the surplus, and rotation of who is released."""

from __future__ import annotations

import datetime as dt
from collections import Counter

from src.allocator.postsolve import relabel_pair_columns
from src.allocator.relievers import plan_relief, row_uid
from src.allocator.windows import SHIFT_NOMINAL_MIN, std_to_ops_day_minutes
from src.schemas import (
    AllocationRow,
    AllocationSheet,
    FlightInput,
    OpsClass,
    Role,
    StaffMember,
)

D = dt.date(2026, 9, 26)


def _staff(eid: str, shift: str, role: Role = Role.STAFF) -> StaffMember:
    return StaffMember(
        employee_id=eid, name=f"S{eid}", role=role, date=D, shift_today=shift,
    )


def _build(staff, per_staff_stds):
    """per_staff_stds: {employee_id: ["HH:MM", ...]} -> rows, flights, matrix."""
    rows, flights = [], []
    n = 0
    for eid, stds in per_staff_stds.items():
        s = next(x for x in staff if x.employee_id == eid)
        for t in stds:
            n += 1
            h, m = map(int, t.split(":"))
            f = FlightInput(
                date=D, flt=str(100 + n), type="J", ac="320", dep="DEL",
                arr="BOM", std=dt.time(h, m), load=100, ops_class=OpsClass.DAY,
            )
            flights.append(f)
            rows.append(AllocationRow(
                date=D, flt=f.flt, dep=f.dep, arr=f.arr, std=f.std, pax=100,
                staff_employee_id=eid, staff_name=s.name,
                sheet_target=AllocationSheet.DAY_OPS,
            ))
    # everybody on a day shift may take any flight (the real matrix is
    # tighter; this isolates the relief logic).
    matrix = {
        f.unique_id: {s.employee_id for s in staff if s.shift_today != "N"}
        for f in flights
    }
    return rows, flights, matrix


def test_one_reliever_each_and_n_has_none():
    staff = (
        [_staff(f"m{i}", "M") for i in range(3)]
        + [_staff(f"a{i}", "A") for i in range(3)]
        + [_staff(f"n{i}", "N") for i in range(3)]
    )
    rows, flights, matrix = _build(
        staff, {s.employee_id: ["08:00"] for s in staff if s.shift_today != "N"},
    )
    _, plan = plan_relief(rows, flights, staff, matrix, D, [])
    assert not plan.freed and not plan.uncovered
    # M and A all relieved; each reliever used once
    assert set(plan.reliever_of) == {s.employee_id for s in staff if s.shift_today != "N"}
    assert max(Counter(plan.reliever_of.values()).values()) == 1
    for sid, rid in plan.reliever_of.items():
        want = {"M": "A", "A": "N"}[next(s for s in staff if s.employee_id == sid).shift_today]
        assert next(s for s in staff if s.employee_id == rid).shift_today == want


def test_surplus_released_early_not_double_booked():
    # 4 A staff, only 2 N relievers -> 2 A are released an hour early.
    staff = (
        [_staff(f"a{i}", "A") for i in range(4)]
        + [_staff(f"n{i}", "N") for i in range(2)]
        + [_staff("a_spare", "A")]
    )
    late = ["20:00", "20:20", "20:40", "20:55"]   # distinct, so spacing allows moves
    stds = {f"a{i}": ["14:00", "17:00", late[i]] for i in range(4)}
    stds["a_spare"] = ["13:30"]
    rows, flights, matrix = _build(staff, stds)
    new_rows, plan = plan_relief(rows, flights, staff, matrix, D, [])
    # 5 A candidates, 2 relievers -> 3 released
    assert len(plan.freed) == 3 and not plan.uncovered
    assert max(Counter(plan.reliever_of.values()).values()) == 1
    assert len(plan.reliever_of) == 2
    end = SHIFT_NOMINAL_MIN["A"][1]
    for sid, cut in plan.freed.items():
        assert cut == end - 60
        for r in new_rows:
            if r.staff_employee_id == sid:
                assert std_to_ops_day_minutes(r.std, r.date, D) <= cut
    # nobody lost a flight
    assert len(new_rows) == len(rows)
    out = relabel_pair_columns(new_rows, flights, staff, [], D, relief=plan)
    named = {}
    for r in out:
        if r.relieved_by_name:
            named.setdefault(r.relieved_by_name, set()).add(r.staff_name)
    assert all(len(v) == 1 for v in named.values())
    # released people carry no reliever on their rows
    for r in out:
        if r.staff_employee_id in plan.freed:
            assert r.relieved_by_employee_id is None


def test_release_rotates_with_history():
    staff = (
        [_staff(f"a{i}", "A") for i in range(4)]
        + [_staff(f"n{i}", "N") for i in range(2)]
    )
    stds = {f"a{i}": ["15:00"] for i in range(4)}
    rows, flights, matrix = _build(staff, stds)
    _, day1 = plan_relief(rows, flights, staff, matrix, D, [])
    assert len(day1.freed) == 2
    hist = {sid: 1 for sid in day1.freed}
    _, day2 = plan_relief(rows, flights, staff, matrix, D, [], history=hist)
    assert set(day2.freed).isdisjoint(day1.freed)


def test_unclearable_person_gets_reliever_instead():
    # a0 holds a late INTL-like (P2F) flight that cannot be moved.
    staff = (
        [_staff(f"a{i}", "A") for i in range(2)]
        + [_staff("n0", "N")]
    )
    rows, flights, matrix = _build(staff, {"a0": ["20:30"], "a1": ["20:30"]})
    # make a0's flight a P2F (never moved)
    f0 = flights[0]
    flights[0] = f0.model_copy(update={"ops_class": OpsClass.P2F})
    matrix = {
        (flights[0].unique_id if k == f0.unique_id else k): v
        for k, v in matrix.items()
    }
    _, plan = plan_relief(rows, flights, staff, matrix, D, [], history={"a1": 5})
    assert "a0" in plan.reliever_of     # can't be freed -> relieved
    assert "a1" in plan.freed


def test_a1_has_no_reliever_and_is_never_released():
    staff = (
        [_staff(f"a1_{i}", "A1") for i in range(4)]
        + [_staff("n0", "N")]
    )
    rows, flights, matrix = _build(
        staff, {f"a1_{i}": ["22:30"] for i in range(4)},
    )
    new_rows, plan = plan_relief(rows, flights, staff, matrix, D, [])
    assert not plan.reliever_of and not plan.freed and not plan.uncovered
    assert [r.staff_employee_id for r in new_rows] == [r.staff_employee_id for r in rows]
