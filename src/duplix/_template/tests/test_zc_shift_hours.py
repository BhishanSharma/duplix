"""Zone Controllers can have their own shift hours (2026-10-16)."""

from datetime import date, time
from types import SimpleNamespace

import pytest

from src.allocator import shift_hours, windows
from src.allocator.eligibility import EligibilityContext, ExcludeReason, check
from src.schemas import OpsClass, Role

D = date(2026, 9, 26)
STAFF_HOURS = {
    "M": ("04:00", "12:30"), "M1": ("06:00", "14:30"), "A": ("12:30", "21:00"),
    "A1": ("14:30", "23:00"), "N": ("20:30", "05:00"),
}


def _raw(**changes):
    out = {s: {"start": a, "end": b} for s, (a, b) in STAFF_HOURS.items()}
    for shift, (a, b) in changes.items():
        out[shift] = {"start": a, "end": b}
    return out


@pytest.fixture
def zc_hours():
    """Apply ZC hours for one test, then go back to following the staff."""
    def apply(**changes):
        windows.apply_zc_hours(shift_hours.to_zc_hours(_raw(**changes)))
    yield apply
    windows.apply_zc_hours(None)


def person(role, shift):
    return SimpleNamespace(
        employee_id="X", role=role, shift_today=shift, is_p2f_licensed=False,
        std_cutoff=None, std_start=None, is_newbie=False,
    )


def flight(h, m, on=D):
    return SimpleNamespace(
        flt="6E1", dep="DEL", std=time(h, m), date=on, ops_class=OpsClass.DAY,
        is_international=False,
    )


def test_zc_follows_staff_hours_by_default():
    assert not windows.zc_hours_customised()
    assert windows.hours_for("A", Role.ZC) == windows.hours_for("A", Role.STAFF)
    assert windows.ZC_BUFFER_START_MIN["M"] == (4 * 60, 5 * 60 + 30)
    assert windows.ZC_BUFFER_END_MIN["N"] == (27 * 60 + 30, 29 * 60)


def test_own_hours_move_the_zc_first_and_last_flight(zc_hours):
    zc_hours(A=("13:30", "22:00"))           # an hour later than A staff
    zc = person(Role.ZC, "A")
    ctx = EligibilityContext(ops_day=D)
    # First flight: 13:30 + 2 h = 15:30 (was 14:30).
    assert check(flight(15, 25), zc, ctx) == ExcludeReason.ZC_START_CUTOFF
    assert check(flight(15, 30), zc, ctx) is None
    # Last flight: 22:00 - 1 h = 21:00 (was 20:00).
    assert check(flight(21, 0), zc, ctx) is None
    assert check(flight(21, 5), zc, ctx) == ExcludeReason.ZC_END_CUTOFF
    # Staff on A keep the staff hours.
    assert check(flight(13, 0), person(Role.STAFF, "A"), ctx) is None
    assert check(flight(21, 15), person(Role.STAFF, "A"), ctx) == ExcludeReason.OUT_OF_SHIFT


def test_own_hours_move_buffers_and_break_relief_hours(zc_hours):
    zc_hours(M=("05:00", "13:00"))
    assert windows.hours_for("M", Role.ZC) == (5 * 60, 13 * 60)
    assert windows.hours_for("M", Role.STAFF) == (4 * 60, 12 * 60 + 30)
    assert windows.ZC_BUFFER_START_MIN["M"] == (5 * 60, 6 * 60 + 30)
    assert windows.ZC_BUFFER_END_MIN["M"] == (11 * 60 + 30, 13 * 60)


def test_reset_follows_staff_again(zc_hours):
    zc_hours(N=("21:00", "05:30"))
    windows.apply_zc_hours(None)
    assert windows.hours_for("N", Role.ZC) == windows.hours_for("N", Role.STAFF)


def test_zc_hours_need_not_form_the_staff_ladder():
    # A ZC shift may start before the previous shift's staff — only each
    # shift on its own is checked.
    hours = shift_hours.to_zc_hours(_raw(M1=("03:30", "12:00")))
    assert hours["M1"] == (3 * 60 + 30, 12 * 60)


@pytest.mark.parametrize("bad,msg", [
    (_raw(A=("13:00", "14:00")), "4–12 hours"),
    (_raw(A=("22:00", "06:00")), "only N runs past midnight"),
    (_raw(A=("13:02", "21:00")), "5-minute steps"),
])
def test_zc_hours_validation(bad, msg):
    with pytest.raises(ValueError, match=msg):
        shift_hours.to_zc_hours(bad)
