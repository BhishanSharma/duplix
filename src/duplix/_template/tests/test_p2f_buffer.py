"""H4 P2F buffer: a handler takes no normal flight from 2 hrs before to
1 hr after each of their P2F flights' STD (eligibility F6)."""

from datetime import date, time
from types import SimpleNamespace

from src.allocator.eligibility import EligibilityContext, ExcludeReason, check
from src.allocator.windows import in_p2f_block
from src.schemas import OpsClass, Role

D = date(2026, 5, 1)
P2F_STD = 10 * 60  # the handler's P2F flight at 10:00


def test_block_runs_from_two_hours_before_to_one_hour_after():
    assert in_p2f_block(8 * 60, [P2F_STD])            # 08:00, edge
    assert not in_p2f_block(8 * 60 - 1, [P2F_STD])    # 07:59
    assert in_p2f_block(11 * 60, [P2F_STD])           # 11:00, edge
    assert not in_p2f_block(11 * 60 + 1, [P2F_STD])   # 11:01
    assert in_p2f_block(13 * 60, [P2F_STD, 14 * 60])  # inside the second P2F's block
    assert not in_p2f_block(9 * 60, [])


def staff(emp="H1"):
    return SimpleNamespace(
        employee_id=emp, role=Role.STAFF, shift_today="M", is_p2f_licensed=True,
        std_cutoff=None, std_start=None, is_newbie=False,
    )


def normal(h, m):
    return SimpleNamespace(
        flt="6E1", std=time(h, m), date=D, ops_class=OpsClass.DAY,
        is_international=False,
    )


def ctx(**kw):
    return EligibilityContext(
        ops_day=D, p2f_handler_by_shift={"M": "H1"},
        p2f_flight_minutes_by_handler={"H1": [P2F_STD]}, **kw,
    )


def test_handler_gets_no_normal_flight_inside_the_block():
    for h, m in ((8, 0), (9, 30), (10, 45), (11, 0)):
        assert check(normal(h, m), staff(), ctx()) == ExcludeReason.P2F_HANDLER_HARD_BLOCK


def test_handler_can_take_normal_flights_outside_the_block():
    assert check(normal(7, 59), staff(), ctx()) is None
    assert check(normal(11, 1), staff(), ctx()) is None


def test_other_staff_are_not_blocked():
    assert check(normal(9, 30), staff("S2"), ctx()) is None


def test_skip_p2f_buffer_override_lets_one_flight_through():
    waived = ctx(skip_p2f_buffer_pairs=frozenset({("6E1|09:30", "H1")}))
    assert check(normal(9, 30), staff(), waived) is None
