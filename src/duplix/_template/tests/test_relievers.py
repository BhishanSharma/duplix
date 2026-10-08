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


# ---------- A1 backup relievers (2026-10-04) ----------

def test_a_leftovers_are_relieved_by_a1_before_anyone_is_released_early():
    # 5 A, only 2 N -> 3 left over; 3 A1 are free to cover them.
    staff = (
        [_staff(f"a{i}", "A") for i in range(5)]
        + [_staff(f"n{i}", "N") for i in range(2)]
        + [_staff(f"x{i}", "A1") for i in range(3)]
    )
    stds = {f"a{i}": ["15:00", "20:30"] for i in range(5)}
    rows, flights, matrix = _build(staff, stds)
    new_rows, plan = plan_relief(rows, flights, staff, matrix, D, [])
    assert not plan.freed and not plan.uncovered
    assert set(plan.reliever_of) == {f"a{i}" for i in range(5)}
    assert len(plan.backup) == 3
    by_id = {s.employee_id: s for s in staff}
    for sid in plan.backup:
        assert by_id[plan.reliever_of[sid]].shift_today == "A1"
    assert max(Counter(plan.reliever_of.values()).values()) == 1
    # the reliever shows up in the RELIEVED_BY column
    out = relabel_pair_columns(new_rows, flights, staff, [], D, relief=plan)
    named = {r.relieved_by_name for r in out if r.relieved_by_name}
    assert {by_id[plan.reliever_of[sid]].name for sid in plan.backup} <= named
    # workload note flags it
    assert all("(A1 backup)" in " ".join(plan.workload_notes[sid]) for sid in plan.backup)


def test_backup_prefers_a1_who_is_free_and_not_already_relieving():
    # m1 is relieved by x_busy (A1) first; the single A leftover must go
    # to the OTHER A1 (x_free), who has no duty and no flight at handover.
    staff = (
        [_staff("m1_0", "M1")]
        + [_staff("x_busy", "A1"), _staff("x_free", "A1")]
        + [_staff(f"a{i}", "A") for i in range(2)]
        + [_staff("n0", "N")]
    )
    stds = {"m1_0": ["08:00"], "a0": ["15:00"], "a1": ["15:00"],
            "x_busy": ["20:50"], "x_free": ["16:00"]}
    rows, flights, matrix = _build(staff, stds)
    _, plan = plan_relief(rows, flights, staff, matrix, D, [])
    assert not plan.uncovered and not plan.freed
    (leftover,) = plan.backup
    # whoever the M1 person got, the backup is a different A1 person
    m1_reliever = plan.reliever_of["m1_0"]
    assert plan.reliever_of[leftover] != m1_reliever


def test_a1_covers_at_most_one_a_person_then_early_release_is_the_fallback():
    # 5 A, 1 N, 2 A1 -> 4 left over; 2 covered by A1, 2 released early.
    staff = (
        [_staff(f"a{i}", "A") for i in range(5)]
        + [_staff("n0", "N")]
        + [_staff(f"x{i}", "A1") for i in range(2)]
    )
    stds = {f"a{i}": ["15:00", "18:00"] for i in range(5)}
    rows, flights, matrix = _build(staff, stds)
    _, plan = plan_relief(rows, flights, staff, matrix, D, [])
    assert len(plan.backup) == 2
    assert len(plan.freed) == 2 and not plan.uncovered
    assert max(Counter(plan.reliever_of.values()).values()) == 1


def test_backup_respects_role():
    staff = (
        [_staff("zc_a", "A", Role.ZC), _staff("zc_a2", "A", Role.ZC)]
        + [_staff("zc_n", "N", Role.ZC)]
        + [_staff("staff_x", "A1", Role.STAFF)]      # wrong role, must not be used
    )
    rows, flights, matrix = _build(staff, {"zc_a": ["15:00"], "zc_a2": ["15:00"]})
    _, plan = plan_relief(rows, flights, staff, matrix, D, [])
    assert not plan.backup
    assert len(plan.freed) == 1
