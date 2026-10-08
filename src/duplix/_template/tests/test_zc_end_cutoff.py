"""H21: zone controllers get no normal flight in the last hour of their
shift (eligibility F5b)."""

from datetime import date, time
from types import SimpleNamespace

import pytest

from src.allocator.eligibility import EligibilityContext, ExcludeReason, check
from src.config import load_config
from src.schemas import OpsClass, Role

D = date(2026, 9, 26)


def person(role, shift):
    return SimpleNamespace(
        employee_id="X", role=role, shift_today=shift, is_p2f_licensed=True,
        std_cutoff=None, std_start=None, is_newbie=False,
    )


def flight(h, m, ops_class=OpsClass.DAY, on=D):
    return SimpleNamespace(
        flt="6E1", dep="DEL", std=time(h, m), date=on, ops_class=ops_class,
        is_international=False,
    )


def ctx(**kw):
    return EligibilityContext(ops_day=D, **kw)


# (shift, last allowed STD, first blocked STD) — nominal end minus 60 min.
@pytest.mark.parametrize("shift,ok,blocked", [
    ("M", (11, 30), (11, 35)),    # ends 12:30
    ("M1", (13, 30), (13, 35)),   # ends 14:30
    ("A", (20, 0), (20, 5)),      # ends 21:00
    ("A1", (22, 0), (22, 5)),     # ends 23:00
])
def test_zc_blocked_after_end_minus_60(shift, ok, blocked):
    zc = person(Role.ZC, shift)
    assert check(flight(*ok), zc, ctx()) is None
    assert check(flight(*blocked), zc, ctx()) == ExcludeReason.ZC_END_CUTOFF


def test_n_zc_cutoff_wraps_midnight():
    zc = person(Role.ZC, "N")      # ends 05:00 D+1 -> last STD 04:00
    nxt = date(2026, 9, 27)
    assert check(flight(4, 0, on=nxt), zc, ctx()) is None
    assert check(flight(4, 5, on=nxt), zc, ctx()) == ExcludeReason.ZC_END_CUTOFF


def test_staff_role_is_unaffected():
    assert check(flight(12, 40), person(Role.STAFF, "M"), ctx()) is None


def test_zc_p2f_flight_is_exempt():
    p2f = flight(12, 0, ops_class=OpsClass.P2F)
    zc = person(Role.ZC, "M")
    c = ctx(p2f_handler_by_shift={"M": "X"})
    assert check(p2f, zc, c) is None


def test_cutoff_can_be_disabled_and_resized():
    zc = person(Role.ZC, "M")
    assert check(flight(12, 40), zc, ctx(zc_end_cutoff_min=0)) is None
    assert check(flight(11, 5), zc, ctx(zc_end_cutoff_min=90)) == (
        ExcludeReason.ZC_END_CUTOFF
    )


def test_shipped_config_sets_60():
    assert load_config("configs/config.yml").zc_cutoff.minutes_before_shift_end == 60
