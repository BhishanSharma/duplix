"""Staffing advice is judged on the busiest 15-minute slots, so it never
suggests removing people the engine needs at a peak."""

from datetime import date, time

from src.pipeline.recommender_staffing import compute_staffing

D = date(2026, 5, 1)


def _at(h, m, n):
    return [(D, time(h, m), "day")] * n


def _spread(n, start_h=5, start_m=10, every=10):
    out = []
    for i in range(n):
        t = start_h * 60 + start_m + i * every
        out.append((D, time(t // 60, t % 60), "day"))
    return out


def _morning(flights, m_staff, **kw):
    rows = compute_staffing(flights, {"M": m_staff}, D, **kw)
    return next(r for r in rows if r.pool == "MORNING")


def test_bunched_peak_is_not_surplus_even_when_volume_is():
    # 28 departures in one slot for 30 people (93%): volume alone would
    # need 2 people, but nobody can be spared at 08:00.
    row = _morning(_at(8, 0, 28), 30)
    assert row.action == "OK"
    assert row.peak_window_flights == 28
    assert row.peak_use_pct == 93
    assert "volume alone" in row.explanation


def test_more_flights_than_people_in_a_slot_means_add():
    row = _morning(_at(8, 0, 32), 30)
    assert row.action == "ADD 2"
    assert row.breakdown_action == "ADD 2 to M"
    assert row.peak_use_pct > 100


def test_light_spread_out_day_can_remove():
    row = _morning(_spread(40), 30)
    assert row.action.startswith("REMOVE")
    assert row.required >= row.volume_floor


def test_squeezed_flights_block_remove():
    row = _morning(_spread(40), 30, squeezed=[(D, time(9, 0))])
    assert row.action == "OK"
    assert row.squeezed == 1
    assert "squeeze" in row.explanation


def test_zcs_stop_an_hour_early():
    # At 12:00 a ZC on M (ends 12:55, cutoff an hour earlier) can't take
    # flights, so 20 flights against 18 STAFF + 4 ZC is over 100%.
    row = _morning(_at(12, 0, 20), 22, zc_by_shift={"M": 4})
    assert row.peak_window_people == 18
    assert row.action.startswith("ADD")
