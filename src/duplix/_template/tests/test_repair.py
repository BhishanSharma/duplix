"""Repair pass: leftover flights placed by moving clashing flights,
never by relaxing a rule."""

from datetime import date, time

from src.allocator.repair import repair_leftovers
from src.allocator.windows import required_spacing_min, spacing_key
from src.schemas import FlightInput, OpsClass, Role, StaffMember

D = date(2026, 5, 1)


def _f(name, h, m):
    return FlightInput(
        date=D, flt=name, type="J", ac="320", dep="DEL", arr="BOM",
        std=time(h, m), load=100, ops_class=OpsClass.DAY,
    )


def _s(eid):
    return StaffMember(
        employee_id=eid, name=eid, role=Role.STAFF, date=D, shift_today="M",
    )


def _spacing_ok(assign, flights):
    by = {f.unique_id: f for f in flights}
    held = {}
    for uid, eid in assign.items():
        held.setdefault(eid, []).append(spacing_key(by[uid], D))
    for keys in held.values():
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if abs(a[0] - b[0]) < required_spacing_min(a, b):
                    return False
    return True


def _apply(assign, summary):
    out = dict(assign)
    out.update(summary.moved)
    out.update(summary.placed)
    return out


def test_one_hop_frees_a_person():
    # A holds 09:00; the leftover 09:05 is only A's. B can take 09:00.
    x, left = _f("X", 9, 0), _f("L", 9, 5)
    flights = [x, left]
    assign = {x.unique_id: "A"}
    matrix = {x.unique_id: {"A", "B"}, left.unique_id: {"A"}}
    s = repair_leftovers([left], assign, flights, [_s("A"), _s("B")], matrix,
                         ops_day=D)
    assert s.placed == {left.unique_id: "A"}
    assert s.moved == {x.unique_id: "B"}
    assert _spacing_ok(_apply(assign, s), flights)


def test_two_hop_chain():
    # L only A; A's X only to B; B's Y only to C.
    x, y, left = _f("X", 9, 0), _f("Y", 9, 5), _f("L", 9, 10)
    flights = [x, y, left]
    assign = {x.unique_id: "A", y.unique_id: "B"}
    matrix = {
        left.unique_id: {"A"}, x.unique_id: {"A", "B"}, y.unique_id: {"B", "C"},
    }
    staff = [_s("A"), _s("B"), _s("C")]
    s = repair_leftovers([left], assign, flights, staff, matrix, ops_day=D)
    final = _apply(assign, s)
    assert final == {left.unique_id: "A", x.unique_id: "B", y.unique_id: "C"}
    assert _spacing_ok(final, flights)


def test_nothing_changes_when_no_chain_exists():
    x, left = _f("X", 9, 0), _f("L", 9, 5)
    assign = {x.unique_id: "A"}
    matrix = {x.unique_id: {"A"}, left.unique_id: {"A"}}
    s = repair_leftovers([left], assign, [x, left], [_s("A")], matrix, ops_day=D)
    assert s.placed == {} and s.moved == {}


def test_forbidden_pair_breaks_released_and_break_are_respected():
    x, left = _f("X", 9, 0), _f("L", 9, 5)
    assign = {x.unique_id: "A"}
    matrix = {x.unique_id: {"A", "B"}, left.unique_id: {"A"}}
    staff = [_s("A"), _s("B")]
    nine = 9 * 60
    for kw in (
        {"forbidden": {(x.unique_id, "B")}},           # post-pass took it off B
        {"blocked_windows": {"B": (nine - 5, nine + 25)}},   # B's break
        {"max_std_by_staff": {"B": nine - 30}},           # B released early
    ):
        s = repair_leftovers([left], assign, [x, left], staff, matrix,
                             ops_day=D, **kw)
        assert s.placed == {} and s.moved == {}, kw


def test_p2f_flights_are_never_moved():
    p = FlightInput(
        date=D, flt="P", type="H", ac="320", dep="DEL", arr="DXB",
        std=time(9, 0), load=0, ops_class=OpsClass.P2F,
    )
    left = _f("L", 9, 5)
    assign = {p.unique_id: "A"}
    matrix = {p.unique_id: {"A", "B"}, left.unique_id: {"A"}}
    s = repair_leftovers([left], assign, [p, left], [_s("A"), _s("B")], matrix,
                         ops_day=D)
    assert s.placed == {} and s.moved == {}
