"""The Setup sidebar's "Shift timings" section.

Changing a shift's hours rewrites the tables in ``allocator/windows.py``
in place and persists to ``configs/shift_times.json``. Every test puts the
shipped hours back afterwards and points the saved file at a temp path, so
the real tables and config are never left changed.
"""

from datetime import date, time

import pytest

from src.allocator import shift_hours, windows


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(shift_hours, "_PATH", tmp_path / "shift_times.json")
    yield
    windows.apply_shift_hours(windows.shipped_shift_hours())


def tables():
    return (
        dict(windows.SHIFT_NOMINAL_MIN), dict(windows.SHIFT_TAIL_END_MIN),
        dict(windows.SHIFT_STD_WINDOW_INNER),
        dict(windows.SHIFT_STD_WINDOW_OUTER_END),
        dict(windows.ZC_BUFFER_START_MIN), dict(windows.ZC_BUFFER_END_MIN),
        dict(windows.AWKWARD_ROUTING_MIN),
    )


def clock(**changes):
    """The shipped hours as the API sends them, with some edits."""
    raw = shift_hours.to_clock(windows.shipped_shift_hours())
    for shift, (start, end) in changes.items():
        raw[shift] = {"start": start, "end": end}
    return raw


def test_shipped_hours_apply_as_a_no_op():
    before = tables()
    windows.apply_shift_hours(windows.shipped_shift_hours())
    assert tables() == before


def test_apply_then_restore_is_exact():
    before = tables()
    windows.apply_shift_hours(shift_hours.to_hours(clock(M=("04:30", "13:00"))))
    assert tables() != before
    windows.apply_shift_hours(windows.shipped_shift_hours())
    assert tables() == before


def test_dependent_tables_move_with_the_shift():
    # M starts 30 min later and ends 30 min later.
    windows.apply_shift_hours(shift_hours.to_hours(clock(M=("04:30", "13:00"))))
    assert windows.SHIFT_NOMINAL_MIN["M"] == (4 * 60 + 30, 13 * 60)
    assert windows.SHIFT_TAIL_END_MIN["M"] == 13 * 60 + 30
    # shipped 05:05-12:55 keeps its offsets from start / end
    assert windows.SHIFT_STD_WINDOW_INNER["M"] == (5 * 60 + 35, 13 * 60 + 25)
    assert windows.ZC_BUFFER_START_MIN["M"] == (4 * 60 + 30, 6 * 60)
    assert windows.ZC_BUFFER_END_MIN["M"] == (11 * 60 + 30, 13 * 60)
    # shifts that were not edited are untouched
    assert windows.SHIFT_NOMINAL_MIN["A"] == (12 * 60 + 30, 21 * 60)


def test_outer_end_follows_the_end_of_a_and_a1():
    windows.apply_shift_hours(shift_hours.to_hours(clock(A=("12:30", "21:30"))))
    assert windows.SHIFT_STD_WINDOW_OUTER_END["A"] == 21 * 60 + 40
    assert windows.SHIFT_STD_WINDOW_OUTER_END["A1"] == 23 * 60  # unchanged


def test_awkward_routing_follows_m_and_m1_ends():
    # M ends 30 min earlier: the 12:55-13:15 group moves with it; the
    # 14:55-15:15 group (M1's) stays.
    windows.apply_shift_hours(shift_hours.to_hours(clock(M=("04:00", "12:00"))))
    assert windows.AWKWARD_ROUTING_MIN[12 * 60 + 25] == ("M",)
    assert 12 * 60 + 55 not in windows.AWKWARD_ROUTING_MIN
    assert windows.AWKWARD_ROUTING_MIN[14 * 60 + 55] == ("A",)
    assert len(windows.AWKWARD_ROUTING_MIN) == 10


def test_time_helpers_use_the_new_hours():
    ops = date(2026, 5, 3)
    # 05:00 is before M's inner window opens (05:05) ...
    assert not windows.is_in_std_window(time(5, 0), ops, ops, "M")
    # ... until M starts 30 min earlier.
    windows.apply_shift_hours(shift_hours.to_hours(clock(M=("03:30", "12:30"))))
    assert windows.is_in_std_window(time(5, 0), ops, ops, "M")


def test_n_may_end_next_morning():
    hours = shift_hours.to_hours(clock(N=("21:00", "05:30")))
    assert hours["N"] == (21 * 60, 24 * 60 + 5 * 60 + 30)


@pytest.mark.parametrize("edit, message", [
    ({"M": ("4:00", "12:30")}, "HH:MM"),             # not zero-padded
    ({"M": ("04:00", "25:00")}, "HH:MM"),
    ({"M": ("04:03", "12:30")}, "5-minute"),
    ({"M": ("12:30", "04:00")}, "only N"),           # wraps midnight
    ({"A1": ("14:30", "02:00")}, "only N"),
    ({"M": ("04:00", "06:00")}, "hours long"),       # 2 h
    ({"M": ("04:00", "17:00")}, "hours long"),       # 13 h
    ({"M1": ("03:00", "12:30")}, "M1 must start after M"),
    ({"M1": ("06:00", "12:45")}, "at least 30 minutes"),
])
def test_bad_hours_are_rejected(edit, message):
    with pytest.raises(ValueError, match=message):
        shift_hours.to_hours(clock(**edit))


@pytest.mark.parametrize("raw", [None, [], {"M": {}}, {"X": {"start": "04:00", "end": "12:30"}}])
def test_malformed_payload_is_rejected(raw):
    with pytest.raises(ValueError):
        shift_hours.to_hours(raw)


def test_missing_shift_is_rejected():
    raw = clock()
    del raw["A1"]
    with pytest.raises(ValueError, match="missing shift"):
        shift_hours.to_hours(raw)


def test_save_load_clear_round_trip():
    assert shift_hours.load_saved() is None
    hours = shift_hours.save(clock(M=("04:30", "13:00")))
    assert shift_hours.load_saved() == hours
    assert hours["M"] == (4 * 60 + 30, 13 * 60)
    shift_hours.clear()
    assert shift_hours.load_saved() is None


def test_a_bad_request_never_touches_the_saved_file():
    shift_hours.save(clock(M=("04:30", "13:00")))
    with pytest.raises(ValueError):
        shift_hours.save(clock(M=("04:03", "13:00")))
    assert shift_hours.load_saved()["M"] == (4 * 60 + 30, 13 * 60)


def test_a_corrupt_saved_file_is_loud_not_ignored():
    shift_hours._PATH.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not usable"):
        shift_hours.load_saved()
