"""Staffing recommender — per-shift required-headcount calculator.

Per user direction 2026-05-22:

  required(S) = max(
      peak_floor(S),            # peak-hour flights / 4 (H10 spacing)
      volume_floor(S),          # total flights / hard_cap (capacity ceiling)
      p2f_floor(S),             # 1 if any P2F flight in shift, else 0
  )

No sick/surge buffer (staff handle that operationally). No NORSE / mentor /
pre-planner floor (user direction: only P2F qualifies as a 'must-fill'
position for the recommender's purpose).

Output schema (see ``StaffingRecommendation`` below) is stored on
``state.staffing`` and surfaced via /api/staffing for the dashboard.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date as date_t
from datetime import time as time_t
from datetime import timedelta
from pathlib import Path

from .schemas import Role, ShiftCode
from .state import AppState

# H10 hard spacing floor (minutes between consecutive same-staff flights).
_H10_SPACING_MIN = 15
# Max flights one staff can handle per hour, derived from H10.
_FLIGHTS_PER_STAFF_PER_HOUR = 60 // _H10_SPACING_MIN  # = 4

# Pool-based primary STD bands. The recommender treats M/M1 and A/A1 as
# shared-coverage pools (since both shifts work overlapping hours and the
# engine allocates across both freely). Output rows = {MORNING, AFTERNOON,
# NIGHT}. The per-shift split inside each pool is reported as a context
# breakdown but the required-headcount math is computed pool-level.
# 2026-05-24 (user direction): AFTERNOON shrunk back to 13:00-21:00 so
# the 21:00-22:55 evening surge (~160 flights on a busy day) lands in
# NIGHT — A1 is still active there but N is the destination shift
# for those legs. Net: AFTERNOON required drops from ~51 to ~44 (in
# line with the 35-36 the assigner expects operationally), NIGHT is
# higher but A1 overlap helps cover the early hours.
_POOL_BANDS_MIN: dict[str, tuple[int, int]] = {
    "MORNING":   (5 * 60 + 5,  13 * 60),
    "AFTERNOON": (13 * 60,     21 * 60),
    "NIGHT":     (21 * 60,     24 * 60 + 5 * 60 + 5),
}
_POOL_SHIFTS: dict[str, tuple[ShiftCode, ...]] = {
    "MORNING":   ("M", "M1"),
    "AFTERNOON": ("A", "A1"),
    "NIGHT":     ("N",),
}
# 2026-05-24 (user direction): A1 stays in AFTERNOON pool but shares
# capacity with N during the 21:00-23:00 overlap. When computing
# NIGHT pool's required headcount, attribute some of the 21:00-23:00
# flight load to A1 (proportional to A1:N staff ratio), so we don't
# over-count N's burden. A1's pool membership and breakdown action
# remain on AFTERNOON only.
_A1_NIGHT_OVERLAP_BAND = (21 * 60, 23 * 60)   # 21:00 - 22:59

# Per-(shift, role) hard caps from configs/shift_limits.json. We pull the
# STAFF cap since the volume floor is computed against the dominant role.
_SHIFT_LIMITS_PATH = Path(__file__).resolve().parents[1] / "configs" / "shift_limits.json"

_SHIFTS: tuple[ShiftCode, ...] = ("M", "M1", "A", "A1", "N")
_POOLS: tuple[str, ...] = ("MORNING", "AFTERNOON", "NIGHT")


@dataclass(frozen=True)
class StaffingRecommendation:
    """One row of the recommendation — per shift pool, with a per-shift
    breakdown so the operator knows exactly which shift to add to or
    remove from."""

    pool: str
    shifts_in_pool: str
    assigned_today: int
    assigned_breakdown: str
    target_breakdown: str             # 2026-05-22: per-shift target headcount after applying recommendation, e.g. "M=28, M1=9"
    breakdown_action: str             # 2026-05-22: actionable per-shift delta, e.g. "REMOVE 6 from M, REMOVE 2 from M1"
    flights_in_window: int
    peak_hour: str
    peak_hour_flights: int
    has_p2f_flight: bool
    target_per_staff: int
    hard_cap_per_staff: int
    peak_floor: int
    volume_floor: int
    p2f_floor: int
    required: int
    bottleneck: str
    gap: int
    action: str
    implied_avg_per_staff: float


def _load_shift_limits() -> dict[str, dict[str, dict[str, int]]]:
    """Read configs/shift_limits.json. Returns the ``shifts`` map."""
    raw = json.loads(_SHIFT_LIMITS_PATH.read_text(encoding="utf-8"))
    return raw["shifts"]


def _std_minutes(std: time_t) -> int:
    """Convert a time-of-day to minutes since midnight."""
    return std.hour * 60 + std.minute


def _flight_in_window(std: time_t, flight_date: date_t, d_day: date_t,
                      window_inner: tuple[int, int]) -> bool:
    """True iff the flight's STD falls within the shift's inner window.

    Window minutes are relative to D-day 00:00; N-shift wraps past midnight
    (end > 24*60), so D+1 flights with STD < 05:00 are treated as ops-day
    minute = std + 24*60.
    """
    start_min, end_min = window_inner
    flt_min = _std_minutes(std)
    if flight_date == d_day + timedelta(days=1):
        flt_min += 24 * 60
    return start_min <= flt_min < end_min


def _hour_label(std: time_t, flight_date: date_t, d_day: date_t) -> str:
    """Bucket label for hourly histogram. D+1 flights display as 24+ to
    keep N-shift contiguous when reading top-to-bottom."""
    h = std.hour
    if flight_date == d_day + timedelta(days=1):
        h += 24
    return f"{h % 24:02d}:00"


def compute_staffing(
    flights: Iterable[tuple[date_t, time_t, str]],
    assigned_by_shift: dict[str, int],
    d_day: date_t,
) -> list[StaffingRecommendation]:
    """Compute required headcount per shift.

    Args:
        flights: iterable of (flight_date, std, ops_class) tuples. ops_class
                 is a lowercase string ('day', 'night', 'p2f', 'norse',
                 'ferry', 'test', 'charter', 'gulf'). Gulf flights are
                 filtered out before the math — extract-only.
        assigned_by_shift: {'M': n, 'M1': n, ...} headcount from the roster
                           (STAFF + ZC, AM excluded).
        d_day: the operating D-day.
    """
    limits = _load_shift_limits()
    # Filter out Gulf (extract-only, never counted toward staffing).
    flights = [(d, s, oc) for (d, s, oc) in flights if oc != "gulf"]

    out: list[StaffingRecommendation] = []
    for pool in _POOLS:
        window = _POOL_BANDS_MIN[pool]
        in_window: list[tuple[date_t, time_t, str]] = [
            (d, s, oc) for (d, s, oc) in flights
            if _flight_in_window(s, d, d_day, window)
        ]
        hourly: Counter[str] = Counter()
        has_p2f = False
        for d, s, oc in in_window:
            hourly[_hour_label(s, d, d_day)] += 1
            if oc == "p2f":
                has_p2f = True
        peak_hour = ""
        peak_count = 0
        if hourly:
            peak_hour, peak_count = hourly.most_common(1)[0]

        lead_shift = _POOL_SHIFTS[pool][0]
        staff_cfg = limits[lead_shift]["STAFF"]
        target = int(staff_cfg["min"])
        hard_cap = int(staff_cfg["max"])

        peak_floor = math.ceil(peak_count / _FLIGHTS_PER_STAFF_PER_HOUR) if peak_count else 0
        # 2026-05-22: volume_floor uses TARGET load, not hard cap.
        # Hard cap (24/22) is the absolute ceiling, but the solver
        # cannot pack everyone to it — H10 spacing, INTL pin windows,
        # P2F buffers, and shift-window edges create slack the math
        # ignores. Using target ensures the recommendation stays
        # operationally feasible. Earlier test (2026-05-22) confirmed
        # cap-based volume_floor caused INFEASIBLE solves when applied.
        effective_flight_count = len(in_window)
        # 2026-05-24: for NIGHT pool, discount A1's helping capacity
        # during the 21:00-23:00 overlap. A1 is still operationally
        # active until 22:55 and shares the load with N before N has
        # to fully cover. We attribute a proportional share of the
        # overlap-window flights to A1 (using its target as effective
        # per-staff capacity over the 2-hour window), so N's required
        # headcount reflects the real night burden.
        if pool == "NIGHT":
            ovl_lo, ovl_hi = _A1_NIGHT_OVERLAP_BAND
            overlap_count = 0
            for d, s, oc in in_window:
                lbl = _hour_label(s, d, d_day)
                hh = int(lbl.split(":")[0])
                # ops-day hour can wrap (00 = midnight)
                std_min_hh = hh * 60
                if ovl_lo <= std_min_hh < ovl_hi:
                    overlap_count += 1
            a1_help = min(
                overlap_count,
                # A1 max contribution during the 2-hour overlap = headcount * target/8hr * 2hr
                int(assigned_by_shift.get("A1", 0) * target / 8 * 2),
            )
            effective_flight_count = max(0, effective_flight_count - a1_help)
            # Peak floor also relaxes if the peak hour falls inside the
            # overlap band — split between A1 and N by staff ratio.
            try:
                peak_hh = int(peak_hour.split(":")[0]) if peak_hour else -1
            except (ValueError, AttributeError):
                peak_hh = -1
            if ovl_lo <= peak_hh * 60 < ovl_hi:
                n_count = assigned_by_shift.get("N", 0)
                a1_count = assigned_by_shift.get("A1", 0)
                total = n_count + a1_count
                if total > 0:
                    n_share = peak_count * n_count / total
                    peak_floor = math.ceil(n_share / _FLIGHTS_PER_STAFF_PER_HOUR)
        volume_floor = math.ceil(effective_flight_count / target) if effective_flight_count and target else 0
        p2f_floor = 1 if has_p2f else 0

        floors = {"peak": peak_floor, "volume": volume_floor, "p2f": p2f_floor}
        required = max(floors.values())
        bottleneck = max(floors, key=lambda k: floors[k])

        pool_shifts = _POOL_SHIFTS[pool]
        assigned_per_shift = {s: assigned_by_shift.get(s, 0) for s in pool_shifts}
        assigned = sum(assigned_per_shift.values())
        breakdown = ", ".join(f"{s}={assigned_per_shift[s]}" for s in pool_shifts)

        gap = required - assigned
        if gap > 0:
            action = f"ADD {gap}"
        elif gap < -2:
            action = f"REMOVE {-gap}"
        else:
            action = "OK"
        implied_avg = (len(in_window) / required) if required else 0.0

        # 2026-05-22: per-shift breakdown of how to apply the recommendation.
        # Distribute the target proportionally to the CURRENT shift ratio
        # so existing M:M1 / A:A1 balance is preserved. For NIGHT (single
        # shift) it's trivial.
        target_per_shift: dict[str, int] = {}
        if len(pool_shifts) == 1:
            target_per_shift[pool_shifts[0]] = required
        elif assigned == 0:
            # Edge case: no one assigned yet — split evenly.
            even = required // len(pool_shifts)
            for s in pool_shifts:
                target_per_shift[s] = even
            target_per_shift[pool_shifts[0]] += required - even * len(pool_shifts)
        else:
            # Proportional to current headcount.
            running = 0
            for s in pool_shifts[:-1]:
                t = round(required * assigned_per_shift[s] / assigned)
                target_per_shift[s] = t
                running += t
            target_per_shift[pool_shifts[-1]] = max(0, required - running)
        target_brk = ", ".join(f"{s}={target_per_shift[s]}" for s in pool_shifts)
        # Build the actionable delta string.
        deltas: list[str] = []
        for s in pool_shifts:
            d = target_per_shift[s] - assigned_per_shift[s]
            if d > 0:
                deltas.append(f"ADD {d} to {s}")
            elif d < 0:
                deltas.append(f"REMOVE {-d} from {s}")
        breakdown_action = " · ".join(deltas) if deltas else "no change"

        out.append(StaffingRecommendation(
            pool=pool,
            shifts_in_pool=" + ".join(pool_shifts),
            assigned_today=assigned,
            assigned_breakdown=breakdown,
            target_breakdown=target_brk,
            breakdown_action=breakdown_action,
            flights_in_window=len(in_window),
            peak_hour=peak_hour,
            peak_hour_flights=peak_count,
            has_p2f_flight=has_p2f,
            target_per_staff=target,
            hard_cap_per_staff=hard_cap,
            peak_floor=peak_floor,
            volume_floor=volume_floor,
            p2f_floor=p2f_floor,
            required=required,
            bottleneck=bottleneck,
            gap=gap,
            action=action,
            implied_avg_per_staff=round(implied_avg, 1),
        ))
    return out


# ---------- state glue ----------


def run_for_state(state: AppState, d_day: date_t) -> list[dict]:
    """Compute the recommendation from current state, store it on
    ``state.staffing`` and return the JSON-friendly rows."""
    flights = [
        (r.date, r.std, r.ops_class.value) for r in state.allocatable_cleaned()
    ]
    # Assigned by shift: STAFF + ZC only (AM excluded — they don't fly).
    assigned: dict[str, int] = {s: 0 for s in _SHIFTS}
    d_iso = d_day.isoformat()
    for av in state.availability:
        if av.date.isoformat() != d_iso or not av.assignable:
            continue
        if av.role is Role.AM:
            continue
        if av.current_shift in assigned:
            assigned[av.current_shift] += 1

    rows = compute_staffing(flights, assigned, d_day)
    out = [asdict(r) for r in rows]
    with state.lock:
        state.staffing = out
    return out
