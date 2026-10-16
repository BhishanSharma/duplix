"""Every next-shift person gets a planner for their first flights, even
when the next shift is bigger than the previous one (2026-10-16)."""

from collections import Counter
from datetime import date

from src.allocator.pair_validation import (
    shared_planner_notes,
    validate_pair_discrepancies,
)
from src.allocator.pairings import generate_pairs
from src.schemas import OpsBoundary, Role, StaffMember

D = date(2026, 9, 26)


def _staff(shift, n, prefix=None):
    prefix = prefix or shift
    return [
        StaffMember(
            employee_id=f"{prefix}{i}", name=f"{prefix}{i}", role=Role.STAFF,
            date=D, shift_today=shift,
        )
        for i in range(n)
    ]


def _planned(pairs, boundary):
    """next employee -> planners, and planner -> how many they plan for."""
    by_next, load = {}, Counter()
    for p in pairs:
        if p.boundary == boundary and p.preplan_count > 0:
            by_next.setdefault(p.next_employee_id, []).append(p.prev_employee_id)
            load[p.prev_employee_id] += 1
    return by_next, load


def test_bigger_next_shift_everyone_gets_a_planner():
    m, a = _staff("M", 35), _staff("A", 39)
    pairs = generate_pairs(m + a)
    by_next, load = _planned(pairs, OpsBoundary.M_TO_A)
    assert set(by_next) == {s.employee_id for s in a}
    assert max(load.values()) == 2          # nobody plans for more than 2
    assert sum(1 for v in load.values() if v == 2) == 4


def test_smaller_next_shift_is_unchanged():
    m, a = _staff("M", 39), _staff("A", 35)
    pairs = generate_pairs(m + a)
    by_next, load = _planned(pairs, OpsBoundary.M_TO_A)
    assert set(by_next) == {s.employee_id for s in a}
    assert max(load.values()) == 1


def test_p2f_handler_plans_for_two_last():
    m, a = _staff("M", 3), _staff("A", 4)
    pairs = generate_pairs(m + a, p2f_handlers={"M": "M0"})
    _, load = _planned(pairs, OpsBoundary.M_TO_A)
    assert load["M0"] == 1
    assert max(load.values()) == 2


def test_m1_to_a1_and_m_to_m1_are_covered():
    staff = _staff("M", 10) + _staff("M1", 12) + _staff("A1", 14)
    pairs = generate_pairs(staff)
    for boundary, nxt in (
        (OpsBoundary.M_TO_M1, "M1"), (OpsBoundary.M1_TO_A1, "A1"),
    ):
        by_next, _ = _planned(pairs, boundary)
        assert set(by_next) == {s.employee_id for s in staff if s.shift_today == nxt}


def test_n_bigger_than_a_is_covered():
    a, n = _staff("A", 3), _staff("N", 5)
    pairs = generate_pairs(a + n)
    by_next, load = _planned(pairs, OpsBoundary.A_TO_N)
    assert set(by_next) == {s.employee_id for s in n}
    assert max(load.values()) == 2


def test_a_bigger_than_n_keeps_the_split_rule():
    a, n, a1 = _staff("A", 6), _staff("N", 3), _staff("A1", 1)
    pairs = generate_pairs(a + n + a1)
    counts = Counter(
        p.preplan_count for p in pairs if p.boundary == OpsBoundary.A_TO_N
    )
    assert counts == Counter({2: 2, 3: 1, 1: 2})


def test_shared_planners_are_noted_not_flagged():
    pairs = generate_pairs(_staff("M", 35) + _staff("A", 39))
    assert validate_pair_discrepancies(pairs, D) == []
    (note,) = shared_planner_notes(pairs, D)
    assert note.code == "W216" and "4 planner(s)" in note.message


def _zcs(shift, n):
    return [
        StaffMember(
            employee_id=f"{shift}z{i}", name=f"{shift}z{i}", role=Role.ZC,
            date=D, shift_today=shift,
        )
        for i in range(n)
    ]


def test_extra_zcs_on_the_next_shift_get_a_planner():
    # More A ZCs than M ZCs, and the extra A ZCs also hand over at A->N,
    # so they never fall into the STAFF pool — they still need a planner.
    staff = _staff("M", 4) + _zcs("M", 1) + _staff("A", 4) + _zcs("A", 3) + _zcs("N", 2)
    pairs = generate_pairs(staff)
    by_next, _ = _planned(pairs, OpsBoundary.M_TO_A)
    assert set(by_next) == {s.employee_id for s in staff if s.shift_today == "A"}


def test_planning_is_spread_evenly_across_staff_and_zcs():
    staff = _staff("M", 1) + _zcs("M", 1) + _staff("A", 2) + _zcs("A", 2)
    pairs = generate_pairs(staff)
    _, load = _planned(pairs, OpsBoundary.M_TO_A)
    assert sorted(load.values()) == [2, 2]


def test_empty_previous_shift_does_not_crash():
    pairs = generate_pairs(_staff("A", 5) + _staff("N", 3))
    by_next, _ = _planned(pairs, OpsBoundary.M_TO_A)
    assert by_next == {}


def test_note_stays_within_the_warning_length_limit():
    # A tiny M shift planning for a big A shift: long name lists.
    m = [
        StaffMember(employee_id=f"m{i}", name=f"MORNING PERSON NUMBER {i}",
                    role=Role.STAFF, date=D, shift_today="M")
        for i in range(3)
    ]
    a = [
        StaffMember(employee_id=f"a{i}", name=f"AFTERNOON PERSON NUMBER {i}",
                    role=Role.STAFF, date=D, shift_today="A")
        for i in range(40)
    ]
    (note,) = shared_planner_notes(generate_pairs(m + a), D)
    assert len(note.message) <= 500
    assert "more (see Pair_Map)" in note.message


def test_m1_helps_before_an_m_person_plans_for_two():
    # M short for A: the 4 extra A people get M1 planners, nobody in M
    # plans for two.
    m, m1, a = _staff("M", 35), _staff("M1", 13, "H"), _staff("A", 39)
    pairs = generate_pairs(m + m1 + a)
    by_next, load = _planned(pairs, OpsBoundary.M_TO_A)
    helped, help_load = _planned(pairs, OpsBoundary.M1_TO_A)
    assert set(by_next) | set(helped) == {s.employee_id for s in a}
    assert len(helped) == 4 and max(load.values()) == 1
    assert max(help_load.values()) == 1
    assert validate_pair_discrepancies(pairs, D) == []
    assert shared_planner_notes(pairs, D) == []


def test_m_shares_again_once_every_m1_person_helps():
    m, m1, a = _staff("M", 3), _staff("M1", 2, "H"), _staff("A", 7)
    pairs = generate_pairs(m + m1 + a)
    by_next, load = _planned(pairs, OpsBoundary.M_TO_A)
    helped, help_load = _planned(pairs, OpsBoundary.M1_TO_A)
    assert set(by_next) | set(helped) == {s.employee_id for s in a}
    assert sorted(help_load.values()) == [1, 1]
    assert sorted(load.values()) == [1, 2, 2]
