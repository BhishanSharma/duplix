"""H20: a nominated P2F handler never takes international flights, so
their day stays free for more P2F work (eligibility F10)."""

from datetime import date, time
from types import SimpleNamespace

from src.allocator.eligibility import EligibilityContext, ExcludeReason, check
from src.schemas import OpsClass, Role

D = date(2026, 5, 1)


def staff(emp="H1", shift="M"):
    return SimpleNamespace(
        employee_id=emp, role=Role.STAFF, shift_today=shift, is_p2f_licensed=True,
        std_cutoff=None, std_start=None, is_newbie=False,
    )


def flight(h, m, intl=False, ops_class=OpsClass.DAY):
    return SimpleNamespace(
        flt="6E1", dep="DEL", std=time(h, m), date=D, ops_class=ops_class,
        is_international=intl,
    )


def ctx(**kw):
    return EligibilityContext(
        ops_day=D, p2f_handler_by_shift={"M": "H1"}, **kw,
    )


def test_nominated_handler_is_blocked_from_international_flights():
    assert check(flight(9, 0, intl=True), staff(), ctx()) == (
        ExcludeReason.P2F_HANDLER_NO_INTL
    )


def test_nominated_handler_can_still_take_domestic_flights():
    assert check(flight(9, 0, intl=False), staff(), ctx()) is None


def test_non_handler_staff_are_not_blocked_from_international():
    assert check(flight(9, 0, intl=True), staff("S2"), ctx()) is None


def test_handler_on_a_shift_with_no_nomination_is_not_blocked():
    # H1 is nominated for M, not A — the same employee working a shift
    # with no P2F nomination is unaffected by M's nomination. (14:00
    # is used so the flight sits in A's own STD window, isolating F10
    # from the unrelated F5 shift-window filter.)
    assert check(flight(14, 0, intl=True), staff("H1", shift="A"), ctx()) is None


def test_p2f_flights_are_unaffected_by_the_intl_block():
    # A P2F flight is never is_international in practice, but make sure
    # the P2F-handler-own-flight path isn't accidentally caught by H20.
    assert check(
        flight(9, 0, intl=False, ops_class=OpsClass.P2F), staff(), ctx(),
    ) is None


def test_handlers_own_p2f_flight_is_never_blocked_even_when_international():
    # Regression: a P2F flight itself CAN be international (e.g. a
    # HAN-CCU rotation). F10 must not swallow the handler's own P2F
    # leg — F4 already governs who may take it, and F10 excluding it
    # here would leave the flight with zero eligible staff.
    assert check(
        flight(9, 0, intl=True, ops_class=OpsClass.P2F), staff(), ctx(),
    ) is None


def test_handlers_normal_international_flight_is_still_blocked():
    # The actual intent of H20: a normal (non-P2F) international
    # flight is blocked for the nominated handler.
    assert check(
        flight(9, 0, intl=True, ops_class=OpsClass.DAY), staff(), ctx(),
    ) == ExcludeReason.P2F_HANDLER_NO_INTL
