"""Pin the shift / spacing / buffer tables in ``allocator/windows.py``.

These numbers drive eligibility, the solver and both post-passes, so an
accidental edit here silently changes every allocation. Plain asserts only.
"""

from datetime import date, time

from src.allocator import windows as w

D = date(2026, 5, 1)
D1 = date(2026, 5, 2)


def t(h, m=0):
    return time(h, m)


# ---------- time arithmetic ----------

def test_ops_day_minutes_same_day_and_next_day():
    assert w.std_to_ops_day_minutes(t(13, 0), D, D) == 780
    assert w.std_to_ops_day_minutes(t(2, 0), D1, D) == 1560  # N continuation


# ---------- structural consistency of the tables ----------

def test_all_tables_cover_the_same_shifts():
    shifts = set(w.SHIFT_NOMINAL_MIN)
    assert shifts == {"M", "M1", "A", "A1", "N"}
    assert set(w.SHIFT_STD_WINDOW_INNER) == shifts
    assert set(w.ZC_BUFFER_START_MIN) == shifts
    assert set(w.ZC_BUFFER_END_MIN) == shifts
    assert set(w.SHIFT_TAIL_END_MIN) == shifts


def test_tail_end_is_nominal_end_plus_handover_window():
    for shift, (_, end) in w.SHIFT_NOMINAL_MIN.items():
        assert w.SHIFT_TAIL_END_MIN[shift] == end + w.HANDOVER_WINDOW_MIN


def test_std_windows_sit_inside_shift_hours_plus_tail():
    for shift, (lo, hi) in w.SHIFT_STD_WINDOW_INNER.items():
        start, _ = w.SHIFT_NOMINAL_MIN[shift]
        tail = w.SHIFT_TAIL_END_MIN[shift]
        outer = w.SHIFT_STD_WINDOW_OUTER_END.get(shift, hi)
        assert lo < hi <= outer
        assert start <= lo, shift
        assert outer <= tail, shift


def test_zc_buffers_hug_the_shift_edges():
    for shift, (start, end) in w.SHIFT_NOMINAL_MIN.items():
        s_lo, s_hi = w.ZC_BUFFER_START_MIN[shift]
        e_lo, e_hi = w.ZC_BUFFER_END_MIN[shift]
        assert s_lo == start and s_lo < s_hi
        assert e_hi == end and e_lo < e_hi
        assert s_hi <= e_lo  # the two buffers never overlap


def test_every_minute_from_0505_to_0500_next_day_has_a_shift():
    """No STD in the operating span may fall between shift windows."""
    for minute in range(5 * 60 + 5, 29 * 60 + 1):
        covered = any(
            lo <= minute <= hi
            for lo, hi in (
                (lo, w.SHIFT_STD_WINDOW_OUTER_END.get(s, hi))
                for s, (lo, hi) in w.SHIFT_STD_WINDOW_INNER.items()
            )
        )
        assert covered, f"minute {minute} not covered by any shift window"


# ---------- STD windows ----------

def test_std_window_edges_morning():
    assert not w.is_in_std_window(t(5, 4), D, D, "M")
    assert w.is_in_std_window(t(5, 5), D, D, "M")
    assert w.is_in_std_window(t(12, 55), D, D, "M")
    assert not w.is_in_std_window(t(12, 56), D, D, "M")


def test_std_window_a_has_soft_outer_band():
    assert not w.is_in_std_window(t(12, 59), D, D, "A")
    assert w.is_in_std_window(t(13, 0), D, D, "A")
    assert w.is_in_std_window(t(21, 5), D, D, "A")
    assert w.is_in_std_window(t(21, 10), D, D, "A")       # outer band
    assert not w.is_in_std_window(t(21, 11), D, D, "A")
    assert not w.is_in_std_outer_band(t(21, 5), D, D, "A")
    assert w.is_in_std_outer_band(t(21, 6), D, D, "A")
    assert w.is_in_std_outer_band(t(21, 10), D, D, "A")
    assert not w.is_in_std_outer_band(t(21, 11), D, D, "A")


def test_outer_band_only_exists_for_a_and_a1():
    assert w.is_in_std_outer_band(t(22, 56), D, D, "A1")
    for shift in ("M", "M1", "N"):
        assert not w.is_in_std_outer_band(t(12, 0), D, D, shift)


def test_night_window_wraps_midnight():
    assert w.is_in_std_window(t(21, 5), D, D, "N")
    assert w.is_in_std_window(t(2, 0), D1, D, "N")
    assert w.is_in_std_window(t(5, 0), D1, D, "N")
    assert not w.is_in_std_window(t(5, 1), D1, D, "N")
    # 02:00 on the ops day itself belongs to the previous night, not this N
    assert not w.is_in_std_window(t(2, 0), D, D, "N")


# ---------- shift hours / handover ----------

def test_is_in_shift_includes_handover_tail():
    assert not w.is_in_shift(t(3, 59), D, D, "M")
    assert w.is_in_shift(t(4, 0), D, D, "M")
    assert w.is_in_shift(t(13, 0), D, D, "M")             # tail end, inclusive
    assert not w.is_in_shift(t(13, 1), D, D, "M")


def test_tail_ext_is_strictly_after_nominal_end():
    assert not w.is_in_tail_ext(t(12, 30), D, D, "M")
    assert w.is_in_tail_ext(t(12, 31), D, D, "M")
    assert w.is_in_tail_ext(t(13, 0), D, D, "M")
    assert not w.is_in_tail_ext(t(13, 1), D, D, "M")


def test_n_shift_tail_ext_current_behaviour():
    """DOCUMENTS CURRENT BEHAVIOUR, not intent.

    Several docstrings/comments say "N has no tail-ext", but
    SHIFT_TAIL_END_MIN adds the uniform 30-min window to N too, so
    05:01-05:30 on D+1 count as N tail-ext. postsolve is unaffected
    (``_RELIEF_NEXT_SHIFT`` has no N entry). If the tail for N is ever
    removed on purpose, flip this assertion.
    """
    assert w.is_in_tail_ext(t(5, 10), D1, D, "N")


# ---------- ZC report buffers (half-open) ----------

def test_zc_buffer_half_open_edges():
    assert w.is_in_zc_buffer(t(4, 0), D, D, "M")
    assert w.is_in_zc_buffer(t(5, 29), D, D, "M")
    assert not w.is_in_zc_buffer(t(5, 30), D, D, "M")
    assert w.is_in_zc_buffer(t(11, 0), D, D, "M")
    assert w.is_in_zc_buffer(t(12, 29), D, D, "M")
    assert not w.is_in_zc_buffer(t(12, 30), D, D, "M")
    assert not w.is_in_zc_buffer(t(8, 0), D, D, "M")


def test_zc_buffer_night_end_is_on_next_day():
    assert w.is_in_zc_buffer(t(3, 30), D1, D, "N")
    assert not w.is_in_zc_buffer(t(3, 29), D1, D, "N")


# ---------- awkward routing ----------

def test_awkward_routing_table():
    assert w.awkward_eligible_shifts(t(13, 0), D, D) == ("M", "M1")
    assert w.awkward_eligible_shifts(t(13, 5), D, D) == ("M1",)
    assert w.awkward_eligible_shifts(t(15, 0), D, D) == ("A",)
    assert w.awkward_eligible_shifts(t(12, 0), D, D) is None


def test_awkward_routing_ignores_next_day_flights():
    assert w.awkward_eligible_shifts(t(13, 0), D1, D) is None


# ---------- H10 spacing ----------

def test_rush_band_edges_are_inclusive():
    assert w.is_in_rush_band(t(5, 0), D, D)
    assert w.is_in_rush_band(t(5, 30), D, D)
    assert not w.is_in_rush_band(t(5, 31), D, D)
    assert w.is_in_rush_band(t(20, 0), D, D)
    assert w.is_in_rush_band(t(22, 0), D, D)
    assert not w.is_in_rush_band(t(22, 1), D, D)


def dom(minutes):
    return (minutes, False, False)


def intl(minutes):
    return (minutes, True, False)


def p2f(minutes, is_intl=False):
    return (minutes, is_intl, True)


def test_spacing_key_reads_std_intl_and_p2f():
    from types import SimpleNamespace

    from src.schemas import OpsClass
    f = SimpleNamespace(std=t(2, 0), date=D1, is_international=True, ops_class=OpsClass.P2F)
    assert w.spacing_key(f, D) == (1560, True, True)


def test_required_spacing_is_15_even_in_a_rush_band():
    assert w.required_spacing_min(dom(20 * 60 + 30), dom(21 * 60)) == 15
    assert w.required_spacing_min(dom(9 * 60), dom(10 * 60)) == 15


def test_required_spacing_is_30_for_domestic_then_intl():
    assert w.required_spacing_min(dom(540), intl(560)) == 30
    assert w.required_spacing_min(intl(560), dom(540)) == 30  # argument order doesn't matter
    assert w.required_spacing_min(intl(540), dom(560)) == 15  # INTL then domestic
    assert w.required_spacing_min(intl(540), intl(560)) == 15


def test_required_spacing_is_30_between_two_p2f_flights():
    assert w.required_spacing_min(p2f(540), p2f(560)) == 30
    assert w.required_spacing_min(p2f(540), dom(560)) == 15   # P2F then normal
    assert w.required_spacing_min(dom(540), p2f(560)) == 15
    assert w.required_spacing_min(dom(540), p2f(560, is_intl=True)) == 30


def test_spacing_clear_checks_every_other_flight():
    others = [dom(9 * 60), intl(11 * 60), p2f(12 * 60)]
    assert w.spacing_clear(dom(10 * 60), others)
    assert not w.spacing_clear(dom(9 * 60 + 14), others)       # 14 min after
    assert not w.spacing_clear(dom(10 * 60 + 31), others)      # 29 min before an INTL
    assert w.spacing_clear(dom(10 * 60 + 30), others)          # exactly 30
    assert not w.spacing_clear(intl(9 * 60 + 29), others)      # INTL 29 min after domestic
    assert not w.spacing_clear(p2f(12 * 60 + 29), others)      # P2F 29 min after a P2F
    assert w.spacing_clear(p2f(12 * 60 + 30), others)
    assert w.spacing_clear(dom(12 * 60 + 15), others)          # normal after P2F: 15


def test_spacing_constants_ordering():
    assert w.SPACING_HARD_MIN < w.SPACING_SOFT_WARN_MIN < w.SPACING_DOM_TO_INTL_MIN
    assert w.SPACING_MAX_MIN == max(w.SPACING_DOM_TO_INTL_MIN, w.SPACING_P2F_PAIR_MIN)


# ---------- shift-boundary proximity ----------

def test_shift_boundary_within_30_min():
    assert w.shift_boundary_within_30min("M", t(4, 30), D, D)       # exactly 30
    assert not w.shift_boundary_within_30min("M", t(4, 31), D, D)
    assert w.shift_boundary_within_30min("N", t(4, 50), D1, D)      # wraps midnight
    assert not w.shift_boundary_within_30min("M", t(8, 0), D, D)
