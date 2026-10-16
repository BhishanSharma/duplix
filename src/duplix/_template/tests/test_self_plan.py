"""A flight that must be planned before its holder's shift starts needs
their handover planner; beyond that, the holder can't take it
(2026-10-16)."""

from datetime import date, time

from src.allocator.self_plan import SelfPlanRule
from src.schemas import (
    FlightInput,
    OpsBoundary,
    OpsClass,
    Pair,
    PairRole,
    Role,
    StaffMember,
)

D = date(2026, 9, 26)


def _f(name, h, m, ops=OpsClass.DAY, dep="DEL"):
    return FlightInput(
        date=D, flt=name, type="J", ac="320", dep=dep, arr="BOM",
        std=time(h, m), load=100, ops_class=ops,
    )


def _a(eid):
    return StaffMember(
        employee_id=eid, name=eid, role=Role.STAFF, date=D, shift_today="A",
    )


def _plan(prev, nxt, count=2):
    return Pair(
        boundary=OpsBoundary.M_TO_A, pair_role=PairRole.PRIMARY,
        preplan_count=count,
        prev_employee_id=prev, prev_name=prev, prev_shift="M",
        next_employee_id=nxt, next_name=nxt, next_shift="A",
    )


# A starts 12:30, so anything before 13:45 is planned before they arrive.
F1, F2, F3, LATE = _f("F1", 13, 0), _f("F2", 13, 20), _f("F3", 13, 40), _f("L", 13, 45)
FLIGHTS = [F1, F2, F3, LATE]


def _rule(pairs):
    return SelfPlanRule(pairs, [_a("A1"), _a("A2")], FLIGHTS, D)


def test_planner_covers_first_flights_and_the_third_goes():
    rule = _rule([_plan("M1", "A1", 2)])
    assign = {F1.unique_id: "A1", F2.unique_id: "A1", F3.unique_id: "A1",
              LATE.unique_id: "A1"}
    assert rule.excess(assign) == [F3.unique_id]


def test_no_planner_means_no_early_flights():
    rule = _rule([])
    assert not rule.ok("A2", F1.unique_id, [])
    assert rule.ok("A2", LATE.unique_id, [])          # 13:45: plans it themselves
    assert rule.latest_self_planned("A2") == "13:45"


def test_ok_counts_what_they_already_hold():
    rule = _rule([_plan("M1", "A1", 2)])
    assert rule.ok("A1", F3.unique_id, [F1.unique_id])
    assert not rule.ok("A1", F3.unique_id, [F1.unique_id, F2.unique_id])


def test_intl_and_p2f_are_exempt():
    intl = _f("I", 13, 0, dep="DXB")
    intl = intl.model_copy(update={"is_international": True})
    p2f = _f("P", 13, 5, ops=OpsClass.P2F)
    rule = SelfPlanRule([], [_a("A2")], [intl, p2f], D)
    assert rule.ok("A2", intl.unique_id, [])
    assert rule.ok("A2", p2f.unique_id, [])


def test_m_shift_is_not_covered():
    m = StaffMember(employee_id="M9", name="M9", role=Role.STAFF, date=D,
                    shift_today="M")
    early = _f("E", 5, 10)
    rule = SelfPlanRule([], [m], [early], D)
    assert rule.ok("M9", early.unique_id, [])


def _m1(eid):
    return StaffMember(
        employee_id=eid, name=eid, role=Role.STAFF, date=D, shift_today="M1",
    )


def test_m1_plans_the_flight_m_cannot():
    from src.allocator.pair_validation import m1_help_notes
    from src.allocator.self_plan import backup_planners
    pairs = [_plan("M1x", "A1", 2)]
    staff = [_a("A1"), _a("A2"), _m1("H1")]
    assign = {F1.unique_id: "A1", F2.unique_id: "A1", F3.unique_id: "A1",
              LATE.unique_id: "A1"}
    extra = backup_planners(pairs, staff, FLIGHTS, D, assign)
    assert [(p.prev_employee_id, p.next_employee_id, p.preplan_count) for p in extra] \
        == [("H1", "A1", 1)]
    rule = SelfPlanRule(pairs + extra, staff, FLIGHTS, D)
    assert rule.excess(assign) == []
    (note,) = m1_help_notes(pairs + extra, D)
    assert note.code == "W217" and "1 M1 person(s) plan 1" in note.message


def test_m1_help_is_capped_per_person():
    from src.allocator.self_plan import M1_HELP_MAX, backup_planners
    # A2 has no planner at all: three early flights, one M1 helper.
    staff = [_a("A2"), _m1("H1")]
    assign = {F1.unique_id: "A2", F2.unique_id: "A2", F3.unique_id: "A2"}
    extra = backup_planners([], staff, FLIGHTS, D, assign)
    assert sum(p.preplan_count for p in extra) == M1_HELP_MAX
    rule = SelfPlanRule(extra, staff, FLIGHTS, D)
    assert rule.excess(assign) == [F3.unique_id]


def test_m1_on_break_at_planning_time_is_not_asked():
    from src.allocator.self_plan import backup_planners
    staff = [_a("A1"), _m1("H1"), _m1("H2")]
    assign = {F1.unique_id: "A1", F2.unique_id: "A1", F3.unique_id: "A1"}
    # F3 departs 13:40, planned 12:25: H1 is on break 12:00-12:30.
    extra = backup_planners([_plan("Mx", "A1", 2)], staff, FLIGHTS, D, assign,
                            breaks={"H1": (12 * 60, 12 * 60 + 30)})
    assert [(p.prev_employee_id, p.preplan_count) for p in extra] == [("H2", 1)]
