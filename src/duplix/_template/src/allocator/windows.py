"""Shift-window tables and time helpers for Step 4.

The numbers below are the single source of truth for H7 (tail extensions),
H9 (ZC report buffers), H10 (15-min spacing), H12 (international ±60min),
and H15 (awkward-window routing). They mirror REF_Constraints in the
unified workbook — keep them in lockstep when either side changes.

Time arithmetic uses "ops-day minutes": minutes from 00:00 of the ops day
(D). A flight at STD 02:00 on calendar D+1 (the N-shift continuation) has
ops_day_minutes = 120 + 1440 = 1560, which is inside N's window
[1230, 1740] = [20:30 D, 05:00 D+1]. This convention sidesteps midnight-
crossing edge cases everywhere downstream.
"""

from __future__ import annotations

from datetime import date as date_t
from datetime import time as time_t

from ..schemas import ShiftCode

# ---------- shift hours (nominal) ----------
# (start_offset, end_offset) in minutes from 00:00 of the ops day.
# N's end is 24*60 + 5*60 = 1740 because it crosses midnight into D+1.

SHIFT_NOMINAL_MIN: dict[ShiftCode, tuple[int, int]] = {
    "M": (4 * 60, 12 * 60 + 30),                 # 04:00 - 12:30
    "M1": (6 * 60, 14 * 60 + 30),                # 06:00 - 14:30
    "A": (12 * 60 + 30, 21 * 60),                # 12:30 - 21:00
    "A1": (14 * 60 + 30, 23 * 60),               # 14:30 - 23:00
    "N": (20 * 60 + 30, 24 * 60 + 5 * 60),       # 20:30 - 05:00 D+1
}

# Handover overlap (round-3 update): for STDs in (nominal_end, nominal_end +
# HANDOVER_WINDOW_MIN], BOTH the prev shift AND the next shift are eligible
# (soft, not hard). The solver picks via S1 count balance + S5 handover
# preference. Postsolve uses is_in_tail_ext (same range) to decide whether
# RELIEVED_BY annotation applies.
HANDOVER_WINDOW_MIN = 30

# ---------- STD distribution windows (user direction 2026-05-12) ----------
# These windows constrain which shifts a flight's STD can be ALLOCATED to.
# Distinct from SHIFT_NOMINAL_MIN (which is staff working hours): a shift
# only takes flights whose STD lies inside this distribution band.
#
# M / M1 / N — hard bounds [inner_lo, inner_hi].
# A / A1    — hard inner band; soft outer extension (extra ~5 min beyond
#             inner_hi). Outer-band assignments are allowed but the solver
#             pays a penalty to minimize their use (S_outer term).
#
# All values are ops-day minutes. N wraps midnight: end=29:00 = 05:00 D+1.
SHIFT_STD_WINDOW_INNER: dict[ShiftCode, tuple[int, int]] = {
    "M":  (5 * 60 + 5,  12 * 60 + 55),                 # 05:05 - 12:55
    "M1": (7 * 60 + 5,  14 * 60 + 55),                 # 07:05 - 14:55
    # Patch 2026-05-14 (post-Phase-4 unallocated fix): A inner_lo was
    # 13:10, leaving a 15-min coverage gap [12:56, 13:09] where only M1
    # (12 staff) could absorb the rush. 9 unallocated 13:00 flights on
    # the 05-03 dataset traced to M1 saturation. Pulling A's left edge
    # to 13:00 lets the much larger A pool (32 STAFF) absorb the rush.
    "A":  (13 * 60,      21 * 60 + 5),                 # 13:00 - 21:05
    "A1": (15 * 60 + 10, 22 * 60 + 55),                # 15:10 - 22:55
    # Same patch on the A->N side: N inner_lo was 21:20, leaving a gap
    # [21:06, 21:19] where only A1 (12 staff) could absorb. Pulling N's
    # left edge to 21:05 lets N share the load.
    "N":  (21 * 60 + 5,  24 * 60 + 5 * 60),            # 21:05 - 05:00 D+1
}
# Soft outer end-extension for A and A1 only. The window grows from
# inner_hi to outer_hi; entries here override that growth for the listed
# shifts. Other shifts have outer = inner (no extension).
SHIFT_STD_WINDOW_OUTER_END: dict[ShiftCode, int] = {
    "A":  21 * 60 + 10,                                # +5 min to 21:10
    "A1": 23 * 60,                                     # +5 min to 23:00
}

# ---------- shift hours with handover window (H7, round-3 update) ----------
# Computed from SHIFT_NOMINAL_MIN + HANDOVER_WINDOW_MIN. Uniform 30 min
# across all shifts — replaces the per-shift D6/r1/r2-1 values.

SHIFT_TAIL_END_MIN: dict[ShiftCode, int] = {
    shift: nominal_end + HANDOVER_WINDOW_MIN
    for shift, (_, nominal_end) in SHIFT_NOMINAL_MIN.items()
}

# ---------- ZC report buffers (H9) ----------
# Half-open intervals [start, end) in ops-day minutes. STDs in either buffer
# are excluded for ZCs of that shift.

ZC_BUFFER_START_MIN: dict[ShiftCode, tuple[int, int]] = {
    "M": (4 * 60, 5 * 60 + 30),                  # 04:00 - 05:30
    "A": (12 * 60 + 30, 14 * 60),                # 12:30 - 14:00
    "M1": (6 * 60, 7 * 60 + 30),                 # 06:00 - 07:30
    "A1": (14 * 60 + 30, 16 * 60),               # 14:30 - 16:00
    "N": (20 * 60 + 30, 22 * 60),                # 20:30 - 22:00
}

ZC_BUFFER_END_MIN: dict[ShiftCode, tuple[int, int]] = {
    "M": (11 * 60, 12 * 60 + 30),                # 11:00 - 12:30
    "A": (19 * 60 + 30, 21 * 60),                # 19:30 - 21:00
    "M1": (13 * 60, 14 * 60 + 30),               # 13:00 - 14:30
    "A1": (21 * 60 + 30, 23 * 60),               # 21:30 - 23:00
    "N": (24 * 60 + 3 * 60 + 30, 24 * 60 + 5 * 60),  # 03:30 - 05:00 D+1
}

# ---------- H15 awkward-window routing ----------
# Keyed by ops-day minutes; value = tuple of shifts eligible at that STD.
# Only applies to flights on the ops day itself (not D+1 N-tail flights —
# those belong to tomorrow's M shift).

AWKWARD_ROUTING_MIN: dict[int, tuple[ShiftCode, ...]] = {
    # M -> A boundary
    12 * 60 + 55: ("M",),
    13 * 60:      ("M", "M1"),
    13 * 60 + 5:  ("M1",),
    13 * 60 + 10: ("M1", "A"),
    13 * 60 + 15: ("A",),
    # M1 -> A1 boundary (per D7 sample evidence: A absorbs entirely)
    14 * 60 + 55: ("A",),
    15 * 60:      ("A",),
    15 * 60 + 5:  ("A",),
    15 * 60 + 10: ("A",),
    15 * 60 + 15: ("A",),
}

# ---------- buffer constants ----------
# H10 (post-round-3 update per user direction 2026-05-10): hard floor
# raised from 10 → 15 min (no two flights for one handler within 15 min);
# soft warning band raised from 15 → 20 min so the assigner sees a flag
# whenever spacing is between 15 and 20 min.
SPACING_HARD_MIN = 15
SPACING_SOFT_WARN_MIN = 20

# H10 band-targeted relaxation (user direction 2026-05-15).
# During the six time bands listed below — operationally-dense rush
# windows — same-staff spacing relaxes to 10 min. Outside these bands
# the 15-min floor still applies. Each entry is (start_min, end_min)
# in ops-day minutes, inclusive. Bands are derived from the manual
# 06-May-2026 allocation's 89 H10 violations (88 of them fall in these
# six windows; one outlier in the 19:40 band has too few violations to
# justify expansion).
SPACING_RELAXED_MIN = 10
H10_RELAXED_BANDS_MIN: tuple[tuple[int, int], ...] = (
    (5 * 60,         5 * 60 + 30),     # 05:00 - 05:30
    (6 * 60 + 55,    7 * 60 + 30),     # 06:55 - 07:30
    (12 * 60 + 55,   13 * 60 + 15),    # 12:55 - 13:15
    (17 * 60,        17 * 60 + 15),    # 17:00 - 17:15
    (20 * 60,        22 * 60),         # 20:00 - 22:00
    (23 * 60 + 15,   23 * 60 + 35),    # 23:15 - 23:35
)


def is_in_h10_relaxed_band(
    std: time_t, flight_date: date_t, ops_day: date_t,
) -> bool:
    """True if a flight's STD lies in one of the H10-relaxed bands.

    For flights in these bands, same-staff spacing of 10 min is allowed
    (rather than the 15-min default). Bands are inclusive on both ends.

    Note: the relaxation is applied via the flight's IntervalVar length
    in the CP-SAT model, so an in-band flight allows 10 min to the
    chronologically NEXT flight regardless of whether the next flight
    is also in a band. Cross-band pairs (one in / one out) get the
    laxer 10 min — a deliberate simplification.
    """
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    return any(lo <= minutes <= hi for lo, hi in H10_RELAXED_BANDS_MIN)


def spacing_floor_min_for_flight(
    std: time_t, flight_date: date_t, ops_day: date_t,
) -> int:
    """Return ``SPACING_RELAXED_MIN`` (10) if the flight's STD is in an
    H10-relaxed band, else ``SPACING_HARD_MIN`` (15). Drives the
    per-flight IntervalVar length in the solver and the post-passes'
    redistribution H10 guard."""
    if is_in_h10_relaxed_band(std, flight_date, ops_day):
        return SPACING_RELAXED_MIN
    return SPACING_HARD_MIN


def _is_in_band_minutes(std_min: int) -> bool:
    """Minutes-only variant of ``is_in_h10_relaxed_band`` — used by the
    post-passes where we only track ops-day-minutes (no time / date
    object) per staff in ``staff_stds``."""
    return any(lo <= std_min <= hi for lo, hi in H10_RELAXED_BANDS_MIN)


def required_spacing_min(std_min_a: int, std_min_b: int) -> int:
    """Required min gap between two same-staff flights at ``std_min_a``
    and ``std_min_b`` (ops-day minutes). Returns 10 when BOTH are in
    relaxed bands, else 15. Matches the solver's per-flight
    IntervalVar length semantics (gap >= max of the two lengths)."""
    in_a = _is_in_band_minutes(std_min_a)
    in_b = _is_in_band_minutes(std_min_b)
    if in_a and in_b:
        return SPACING_RELAXED_MIN
    return SPACING_HARD_MIN


def shift_boundary_within_30min(
    shift: ShiftCode, std: time_t, flight_date: date_t, ops_day: date_t,
) -> bool:
    """True iff ``shift``'s nominal START or END lies within ±30 min of
    the flight's STD.

    Used by the band-targeted "prefer stable shift" soft preference
    (user direction 2026-05-15): within the H10-relaxed bands, the
    solver should prefer to assign a flight to a staff whose shift is
    NOT in transition. A shift is "in transition" at STD T when its
    nominal start or end is within 30 minutes of T.

    Handles N's midnight wrap: N's nominal end is at ops-day-min 1740
    (= 05:00 D+1). A D-day flight at 04:50 (ops-min 290) is far from
    that 1740 in the linear sense, but std_to_ops_day_minutes adds
    1440 when ``flight_date == ops_day + 1``, so the comparison stays
    correct on either side of midnight.
    """
    nominal = SHIFT_NOMINAL_MIN.get(shift)
    if nominal is None:
        return False
    start_min, end_min = nominal
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    return abs(minutes - start_min) <= 30 or abs(minutes - end_min) <= 30

# H4 P2F buffer (round-3 rewrite): per-handler exclusion windows around
# each P2F flight's STD. Hardened in eligibility.py's F4. Constants here
# mirror what's in REF_Constraints H4.
P2F_HARD_BLOCK_BEFORE_HRS = 3   # ±15 min around STD-3hrs: hard exclusion
P2F_PARTIAL_BEFORE_HRS = 1      # ±15 min around STD-1hr: max 1 normal flight
P2F_PARTIAL_AFTER_MIN = 20      # ±15 min around STD+20min: max 1 normal flight
P2F_WINDOW_HALF_MIN = 15        # half-width of each window

# International ±60 min from shift start/end (H12, correction r2-3).
INTL_SHIFT_EDGE_MIN = 60


# ---------- time-arithmetic helpers ----------

def std_to_ops_day_minutes(std: time_t, flight_date: date_t, ops_day: date_t) -> int:
    """Convert (calendar date, STD) to minutes-from-00:00-of-ops-day.

    A 02:00 STD on calendar (ops_day + 1) returns 1560, which N's window
    [1230, 1740] correctly contains.
    """
    base = std.hour * 60 + std.minute
    delta_days = (flight_date - ops_day).days
    return base + delta_days * 1440


def is_in_shift(
    std: time_t, flight_date: date_t, ops_day: date_t, shift: ShiftCode,
) -> bool:
    """True if (flight_date, std) falls within ``shift``'s window+tail-ext on
    ``ops_day``. Inclusive of both endpoints — a 13:00 STD is still M's
    (M's tail ends at 13:00 sharp, so STD=13:00 is the last allowed)."""
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    start = SHIFT_NOMINAL_MIN[shift][0]
    end = SHIFT_TAIL_END_MIN[shift]
    return start <= minutes <= end


def is_in_zc_buffer(
    std: time_t, flight_date: date_t, ops_day: date_t, shift: ShiftCode,
) -> bool:
    """True if STD is in the ZC's start-of-shift OR end-of-shift report
    buffer for ``shift``. Half-open [start, end), so the buffer's upper
    edge is the first eligible STD."""
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    s1, e1 = ZC_BUFFER_START_MIN[shift]
    s2, e2 = ZC_BUFFER_END_MIN[shift]
    return (s1 <= minutes < e1) or (s2 <= minutes < e2)


def awkward_eligible_shifts(
    std: time_t, flight_date: date_t, ops_day: date_t,
) -> tuple[ShiftCode, ...] | None:
    """Return the tuple of shifts allowed at this STD per H15, OR None if
    the STD is not in any awkward window. D+1 flights never trigger H15
    (next-day's M owns them, not today's solver)."""
    if flight_date != ops_day:
        return None
    minutes = std.hour * 60 + std.minute
    return AWKWARD_ROUTING_MIN.get(minutes)


def is_in_tail_ext(
    std: time_t, flight_date: date_t, ops_day: date_t, shift: ShiftCode,
) -> bool:
    """True if STD is in the tail-extension portion of ``shift`` —
    i.e., past the nominal end but within the configured tail end.

    Tail-ext flights are still owned by ``shift`` (STAFF column points
    to the prev-shift handler), but the next shift's paired person does
    the post-airborne (D+20) work and is recorded as RELIEVED_BY.
    Flights at or before nominal end are NOT relieved (the prev staff
    handles their own D+20 within shift). N has no tail extension —
    nominal end == tail end, so this returns False for any N flight.
    """
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    nominal_end = SHIFT_NOMINAL_MIN[shift][1]
    tail_end = SHIFT_TAIL_END_MIN[shift]
    return nominal_end < minutes <= tail_end


def is_within_intl_shift_edge(
    std: time_t, flight_date: date_t, ops_day: date_t, shift: ShiftCode,
) -> bool:
    """H12 (correction r2-3): for international flights, exclude staff
    whose STD is within 60 min of their shift's nominal start or end.

    Strict-less-than / strict-greater-than per the correction's literal
    wording: STD < shift_start + 60min OR STD > shift_end - 60min.

    Retired 2026-05-12 (F9 dropped in eligibility.py). Kept here for
    history / tests; not called by the live pipeline.
    """
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    start, end = SHIFT_NOMINAL_MIN[shift]
    return minutes < start + INTL_SHIFT_EDGE_MIN or minutes > end - INTL_SHIFT_EDGE_MIN


def is_in_std_window(
    std: time_t, flight_date: date_t, ops_day: date_t, shift: ShiftCode,
) -> bool:
    """True if the flight's STD lies in ``shift``'s STD distribution
    window (inner OR outer band; outer applies to A and A1 only).

    Per user direction 2026-05-12 §1: a flight is eligible for a shift
    only when its STD falls in that shift's STD window. This is a
    NARROWER check than ``is_in_shift`` (which spans the full shift
    hours + handover overlap). Replaces F5 in eligibility.py.
    """
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    lo, hi = SHIFT_STD_WINDOW_INNER[shift]
    outer_hi = SHIFT_STD_WINDOW_OUTER_END.get(shift, hi)
    return lo <= minutes <= outer_hi


def is_in_std_outer_band(
    std: time_t, flight_date: date_t, ops_day: date_t, shift: ShiftCode,
) -> bool:
    """True if STD lies in the soft outer band (inner_hi, outer_hi].

    Used by the solver to penalize outer-band assignments via a soft
    objective term — A and A1 only have non-empty bands. Other shifts
    return False (no outer band defined).
    """
    if shift not in SHIFT_STD_WINDOW_OUTER_END:
        return False
    minutes = std_to_ops_day_minutes(std, flight_date, ops_day)
    _, inner_hi = SHIFT_STD_WINDOW_INNER[shift]
    outer_hi = SHIFT_STD_WINDOW_OUTER_END[shift]
    return inner_hi < minutes <= outer_hi
