"""Regression tests for the 2026-10-10 allocation fixes: N-shift cutoff
across midnight, P2F handler buffer for overlapping shift windows, the
spread exemption for capped staff, pin-clash release, and greedy
fallback keeping pins."""

from collections import Counter
from datetime import date, time, timedelta
from types import SimpleNamespace

from src.allocator.eligibility import EligibilityContext, ExcludeReason, check
from src.allocator.greedy_fallback import greedy_allocate
from src.schemas import FlightInput, OpsClass, Role, StaffMember
from src.solver.allocator_cpsat import solve_allocation
from src.pipeline.step3_allocate_flights import _build_p2f_handler_anchors

D = date(2026, 5, 1)


# ---------- N-shift cutoff_time across midnight ----------

def _n_staff(**kw):
    return SimpleNamespace(
        employee_id="N1", role=Role.STAFF, shift_today="N",
        is_p2f_licensed=False, is_newbie=False,
        std_cutoff=kw.get("std_cutoff"), std_start=kw.get("std_start"),
    )


def _flight(h, m, day=D):
    return SimpleNamespace(
        flt="6E1", std=time(h, m), date=day, ops_class=OpsClass.NIGHT,
        is_international=False,
    )


def _ctx():
    return EligibilityContext(ops_day=D)


def test_n_cutoff_after_midnight_keeps_evening_flights():
    s = _n_staff(std_cutoff=time(1, 0))
    assert check(_flight(22, 0), s, _ctx()) is None
    assert check(_flight(0, 50, D + timedelta(days=1)), s, _ctx()) is None
    assert (
        check(_flight(2, 0, D + timedelta(days=1)), s, _ctx())
        == ExcludeReason.OVERRIDE_CUTOFF
    )


def test_n_cutoff_before_midnight_drops_next_day_flights():
    s = _n_staff(std_cutoff=time(23, 0))
    assert check(_flight(22, 30), s, _ctx()) is None
    assert (
        check(_flight(2, 0, D + timedelta(days=1)), s, _ctx())
        == ExcludeReason.OVERRIDE_CUTOFF
    )


def test_n_start_after_midnight():
    s = _n_staff(std_start=time(0, 30))
    assert check(_flight(22, 0), s, _ctx()) == ExcludeReason.OVERRIDE_CUTOFF
    assert check(_flight(1, 0, D + timedelta(days=1)), s, _ctx()) is None


# ---------- P2F handler buffer in overlapping shift windows ----------

def _p2f(h, m):
    return SimpleNamespace(
        std=time(h, m), date=D, ops_class=OpsClass.P2F,
    )


def test_p2f_in_m1_overlap_buffers_the_a_handler():
    anchors = _build_p2f_handler_anchors(
        [_p2f(13, 20)], {"A": ["HA"], "M": ["HM"]}, D,
    )
    assert anchors == {"HA": [13 * 60 + 20]}


def test_p2f_in_a1_overlap_buffers_the_n_handler():
    anchors = _build_p2f_handler_anchors(
        [_p2f(22, 30)], {"N": ["HN"], "A": ["HA"]}, D,
    )
    assert anchors == {"HN": [22 * 60 + 30]}


# ---------- solver ----------

def _fi(i, minute):
    return FlightInput(
        date=D, flt=f"6E{i}", type="J", ac="320", dep="DEL", arr="BOM",
        std=time(minute // 60, minute % 60), load=100, ops_class=OpsClass.DAY,
    )


def _sm(eid, cap=None):
    return StaffMember(
        employee_id=eid, name=eid, role=Role.STAFF, date=D,
        shift_today="M", max_flights_cap=cap,
    )


def _setup(n_flights=30):
    # 20 min apart from 05:10, so any one person can take any of them.
    flights = [_fi(i, 5 * 60 + 10 + 20 * i) for i in range(n_flights)]
    staff = [_sm("S1"), _sm("S2"), _sm("CAPPED", cap=2)]
    elig = {f.unique_id: {s.employee_id for s in staff} for f in flights}
    return flights, staff, elig


def test_capped_staff_no_longer_throttle_the_bucket():
    flights, staff, elig = _setup()
    kw = dict(
        ops_day=D, max_seconds=20, enable_s1_count_balance=True,
        day_level_by_bucket={("M", Role.STAFF): 14},
        hard_bucket_spread_max=2,
    )
    throttled = solve_allocation(flights, staff, elig, **kw)
    assert len(throttled.assignments) < len(flights)  # the old behaviour

    fixed = solve_allocation(
        flights, staff, elig, spread_exempt_ids=frozenset({"CAPPED"}), **kw,
    )
    assert fixed.status in ("OPTIMAL", "FEASIBLE")
    assert len(fixed.assignments) == len(flights)
    per = {e: list(fixed.assignments.values()).count(e) for e in ("S1", "S2")}
    assert abs(per["S1"] - per["S2"]) <= 1


def test_no_hard_ceiling_below_cap_leaves_flights_unallocated():
    # Day level 10 used to mean a HARD 11 per person: 2 x 11 < 26 flights.
    flights, staff, elig = _setup(26)
    staff = staff[:2]
    elig = {u: {"S1", "S2"} for u in elig}
    res = solve_allocation(
        flights, staff, elig, ops_day=D, max_seconds=20,
        enable_s1_count_balance=True,
        day_level_by_bucket={("M", Role.STAFF): 10},
    )
    assert len(res.assignments) == 26


def test_clashing_pins_are_released_not_infeasible():
    a = _fi(1, 6 * 60)
    b = _fi(2, 6 * 60 + 5)   # 5 min later — H10 forbids both on one person
    staff = [_sm("S1"), _sm("S2")]
    elig = {a.unique_id: {"S1", "S2"}, b.unique_id: {"S1", "S2"}}
    res = solve_allocation(
        [a, b], staff, elig, ops_day=D, max_seconds=10,
        pinned_assignments={a.unique_id: "S1", b.unique_id: "S1"},
    )
    assert res.status in ("OPTIMAL", "FEASIBLE")
    assert res.assignments[a.unique_id] == "S1"
    assert res.assignments[b.unique_id] == "S2"


def test_greedy_fallback_keeps_pins():
    flights, staff, elig = _setup(6)
    pins = {flights[0].unique_id: "S2", flights[1].unique_id: "S2"}
    out = greedy_allocate(flights, staff, elig, ops_day=D, preassigned=pins)
    assert out[flights[0].unique_id] == "S2"
    assert out[flights[1].unique_id] == "S2"
    assert len(out) == 6


# ---------- second rebalance (after force-place) ----------

def _rows_for(flights, owner):
    from src.schemas import AllocationRow, AllocationSheet
    return [
        AllocationRow(
            date=f.date, flt=f.flt, dep=f.dep, arr=f.arr, std=f.std,
            pax=f.load, staff_employee_id=owner, staff_name=owner,
            sheet_target=AllocationSheet.DAY_OPS,
        )
        for f in flights
    ]


def _rebalance(**kw):
    from src.allocator.postpass_rebalance import apply_rebalance_pass
    flights, staff, elig = _setup(6)
    staff = staff[:2]  # S1 holds all 6, S2 holds none
    elig = {u: {"S1", "S2"} for u in elig}
    rows, _ = apply_rebalance_pass(
        _rows_for(flights, "S1"), flights, staff, elig,
        p2f_handlers={}, d_day=D, **kw,
    )
    return Counter(r.staff_employee_id for r in rows)


def test_rebalance_levels_by_default():
    assert _rebalance() == Counter({"S1": 3, "S2": 3})


def test_rebalance_respects_early_release_and_break():
    # S2 released at 05:20 -> only the 05:10 flight is still allowed.
    assert _rebalance(max_std_by_staff={"S2": 5 * 60 + 20})["S2"] == 1
    # S2's break covers every flight -> nothing moves.
    assert _rebalance(blocked_windows={"S2": (0, 24 * 60)})["S2"] == 0


def test_rebalance_never_moves_frozen_rows():
    flights, _staff, _elig = _setup(6)
    frozen = frozenset(f.unique_id for f in flights)
    assert _rebalance(frozen_uids=frozen)["S2"] == 0


# ---------- warnings across repeated Allocate runs ----------

def test_rerunning_allocate_replaces_its_warnings():
    from src.schemas import Severity, WarningRow
    from src.state import AppState
    from src.pipeline.step3_allocate_flights import _store_warnings

    st = AppState()
    plan_w = WarningRow(severity=Severity.WARN, code="W020", message="plan")
    st.warnings = [plan_w]
    first = [WarningRow(severity=Severity.WARN, code="W230", message="a")]
    _store_warnings(st, first, append=True)
    second = [WarningRow(severity=Severity.WARN, code="W230", message="b")]
    _store_warnings(st, second, append=True)
    assert [w.message for w in st.warnings] == ["plan", "b"]
