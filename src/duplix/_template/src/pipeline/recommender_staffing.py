"""Staffing recommender — how many people each shift pool needs.

2026-10-11 rewrite. The old version divided the busiest HOUR by 4
(one flight per 15 min per person) and the pool's flights by the
per-person target. Both are averages, and they told the client to
"REMOVE 7 from M" on days the engine was already squeezing flights:
departures bunch inside the hour (40+ flights in one 15-minute slot),
and at those minutes nearly everyone who can legally take a flight is
already busy.

What it measures now
--------------------
For every 15-minute slot of the day (stepping 5 min) it counts the
departures in the slot and the people who could take at least one of
them (shift STD window; ZCs stop an hour before their shift ends). A
person can take at most ONE flight in any 15-minute slot (H10), so:

* use > 100%  — more flights than people: the engine MUST squeeze
                (relax spacing) there. -> ADD.
* use > ~85%  — tight: on real days the engine started squeezing from
                ~89% (INTL needs 30 min, P2F blocks, breaks and early
                releases eat into the headroom).
* REMOVE is only suggested when, after removing people, every slot the
  shift covers stays at or below 85% AND total volume still fits the
  per-person targets AND the last Allocate did not have to squeeze a
  flight in that pool's hours.

Output rows = {MORNING, AFTERNOON, NIGHT}; the per-shift breakdown says
exactly which shift to add to or remove from.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date as date_t
from datetime import time as time_t
from pathlib import Path

from ..allocator.windows import (
    SHIFT_STD_WINDOW_INNER,
    SHIFT_STD_WINDOW_OUTER_END,
    std_to_ops_day_minutes,
)
from ..schemas import Role, ShiftCode
from ..state import AppState

# One person can take at most one flight in any 15-minute slot (H10).
_SLOT_MIN = 15
_SLOT_STEP = 5
# Utilisation (flights / people who can take them) at or below which a
# slot is comfortable. Calibrated on two real 2,100-flight days: no
# squeezed flight at <= 85%, squeezing from ~89% upward.
SAFE_USE = 0.85
# ZCs take no normal flight in their last hour (H21).
_ZC_CUTOFF_MIN = 60
# Below this many spare people a pool is reported as OK, not REMOVE.
_REMOVE_MIN = 3

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
# A1 works until 22:55, so it shares the NIGHT pool's early hours.
_A1_NIGHT_OVERLAP_BAND = (21 * 60, 23 * 60)

_SHIFT_LIMITS_PATH = Path(__file__).resolve().parents[2] / "configs" / "shift_limits.json"

_SHIFTS: tuple[ShiftCode, ...] = ("M", "M1", "A", "A1", "N")
_POOLS: tuple[str, ...] = ("MORNING", "AFTERNOON", "NIGHT")
_ROLES: tuple[str, ...] = ("STAFF", "ZC")

Group = tuple[str, str]   # (shift, role)


@dataclass(frozen=True)
class StaffingRecommendation:
    """One row per shift pool, with a per-shift breakdown."""

    pool: str
    shifts_in_pool: str
    assigned_today: int
    assigned_breakdown: str
    target_breakdown: str
    breakdown_action: str
    flights_in_window: int
    peak_window: str              # busiest 15-min slot, e.g. "21:15–21:29"
    peak_window_flights: int
    peak_window_people: int       # people who could take those flights
    peak_use_pct: int             # flights / people, %
    tight_windows: str            # other slots above SAFE_USE
    has_p2f_flight: bool
    target_per_staff: int
    hard_cap_per_staff: int
    volume_floor: int
    p2f_floor: int
    required: int
    comfortable: int              # headcount for every slot <= SAFE_USE
    bottleneck: str
    gap: int
    action: str
    squeezed: int                 # flights the last Allocate had to squeeze here
    squeezed_times: str
    explanation: str
    implied_avg_per_staff: float


def _load_shift_limits() -> dict[str, dict[str, dict[str, int]]]:
    raw = json.loads(_SHIFT_LIMITS_PATH.read_text(encoding="utf-8"))
    return raw["shifts"]


def _hm(minute: int) -> str:
    return f"{(minute // 60) % 24:02d}:{minute % 60:02d}"


def _slot_label(t: int) -> str:
    return f"{_hm(t)}–{_hm(t + _SLOT_MIN - 1)}"


def _covers(group: Group, minute: int) -> bool:
    shift, role = group
    lo, hi = SHIFT_STD_WINDOW_INNER[shift]
    hi = SHIFT_STD_WINDOW_OUTER_END.get(shift, hi)
    if role == "ZC":
        hi -= _ZC_CUTOFF_MIN
    return lo <= minute <= hi


@dataclass
class _Slot:
    start: int
    flights: int
    groups: frozenset          # (shift, role) groups that can take any of them

    def people(self, heads: dict[Group, int]) -> int:
        return sum(heads.get(g, 0) for g in self.groups)


def _slots(minutes: list[int]) -> list[_Slot]:
    out: list[_Slot] = []
    if not minutes:
        return out
    ms = sorted(minutes)
    all_groups = [(s, r) for s in _SHIFTS for r in _ROLES]
    t = ms[0] - ms[0] % _SLOT_STEP
    while t <= ms[-1]:
        inside = [m for m in ms if t <= m < t + _SLOT_MIN]
        if inside:
            groups = frozenset(
                g for g in all_groups if any(_covers(g, m) for m in inside)
            )
            out.append(_Slot(t, len(inside), groups))
        t += _SLOT_STEP
    return out


def _use(slot: _Slot, heads: dict[Group, int]) -> float:
    p = slot.people(heads)
    return slot.flights / p if p else float("inf")


def _add_until(
    slots: list[_Slot], heads: dict[Group, int], pool_shifts: tuple[str, ...],
    limit: float,
) -> dict[str, int]:
    """Add STAFF to the pool's shifts until every slot is <= ``limit``.
    Returns the additions per shift. Slots no pool shift covers are
    skipped (another pool is responsible for them)."""
    heads = dict(heads)
    added: dict[str, int] = {}
    for _ in range(200):
        worst = None
        for s in slots:
            cands = [sh for sh in pool_shifts if (sh, "STAFF") in s.groups]
            if not cands or _use(s, heads) <= limit:
                continue
            if worst is None or _use(s, heads) > _use(worst[0], heads):
                worst = (s, cands)
        if worst is None:
            break
        shift = worst[1][0]
        heads[(shift, "STAFF")] = heads.get((shift, "STAFF"), 0) + 1
        added[shift] = added.get(shift, 0) + 1
    return added


def _removable(
    slots: list[_Slot], heads: dict[Group, int], pool_shifts: tuple[str, ...],
    min_headcount: int,
) -> dict[str, int]:
    """STAFF that can come off the pool's shifts while every slot they
    cover stays <= SAFE_USE and the pool keeps ``min_headcount``."""
    heads = dict(heads)
    removed: dict[str, int] = {}
    pool_total = sum(heads.get((s, r), 0) for s in pool_shifts for r in _ROLES)
    while pool_total > min_headcount:
        best = None
        for shift in pool_shifts:
            g = (shift, "STAFF")
            if heads.get(g, 0) <= 1:
                continue
            heads[g] -= 1
            worst = max(
                (_use(s, heads) for s in slots if g in s.groups), default=0.0,
            )
            heads[g] += 1
            if worst <= SAFE_USE and (best is None or worst < best[1]):
                best = (shift, worst)
        if best is None:
            break
        heads[(best[0], "STAFF")] -= 1
        removed[best[0]] = removed.get(best[0], 0) + 1
        pool_total -= 1
    return removed


def compute_staffing(
    flights: Iterable[tuple[date_t, time_t, str]],
    assigned_by_shift: dict[str, int],
    d_day: date_t,
    *,
    zc_by_shift: dict[str, int] | None = None,
    squeezed: Iterable[tuple[date_t, time_t]] = (),
) -> list[StaffingRecommendation]:
    """Required headcount per shift pool.

    ``flights``: (flight_date, std, ops_class) — Gulf is dropped here.
    ``assigned_by_shift``: STAFF + ZC on shift (AM excluded).
    ``zc_by_shift``: how many of those are ZCs (they stop an hour early
    and carry a lower target). ``squeezed``: (date, std) of flights the
    last Allocate had to place by relaxing a rule.
    """
    limits = _load_shift_limits()
    zc = zc_by_shift or {}
    minutes = [
        std_to_ops_day_minutes(s, d, d_day)
        for (d, s, oc) in flights if oc != "gulf"
    ]
    p2f_minutes = [
        std_to_ops_day_minutes(s, d, d_day)
        for (d, s, oc) in flights if oc == "p2f"
    ]
    squeezed_min = sorted(std_to_ops_day_minutes(s, d, d_day) for d, s in squeezed)

    heads: dict[Group, int] = {}
    for shift in _SHIFTS:
        n_zc = min(zc.get(shift, 0), assigned_by_shift.get(shift, 0))
        heads[(shift, "ZC")] = n_zc
        heads[(shift, "STAFF")] = assigned_by_shift.get(shift, 0) - n_zc

    all_slots = _slots(minutes)

    out: list[StaffingRecommendation] = []
    for pool in _POOLS:
        lo, hi = _POOL_BANDS_MIN[pool]
        pool_shifts = _POOL_SHIFTS[pool]
        lead = pool_shifts[0]
        slots = [s for s in all_slots if lo <= s.start < hi]
        in_band = [m for m in minutes if lo <= m < hi]
        has_p2f = any(lo <= m < hi for m in p2f_minutes)

        staff_target = int(limits[lead]["STAFF"]["min"])
        zc_target = int(limits[lead]["ZC"]["min"])
        hard_cap = int(limits[lead]["STAFF"]["max"])

        # Volume: everyone at their own target must cover the pool's
        # flights. At night A1 still covers 21:00-22:55; credit its spare
        # capacity there so N isn't over-counted.
        effective = len(in_band)
        if pool == "NIGHT":
            o_lo, o_hi = _A1_NIGHT_OVERLAP_BAND
            overlap = sum(1 for m in in_band if o_lo <= m < o_hi)
            a1_help = int(assigned_by_shift.get("A1", 0) * staff_target / 8 * 2)
            effective = max(0, effective - min(overlap, a1_help))
        pool_zc = sum(heads[(s, "ZC")] for s in pool_shifts)
        volume_floor = (
            pool_zc + max(0, math.ceil((effective - pool_zc * zc_target) / staff_target))
            if effective else 0
        )
        p2f_floor = 1 if has_p2f else 0

        assigned_per_shift = {s: assigned_by_shift.get(s, 0) for s in pool_shifts}
        assigned = sum(assigned_per_shift.values())

        # Busiest slot and the tight ones.
        def use_pct(s: _Slot) -> int:
            p = s.people(heads)
            return round(100 * s.flights / p) if p else 999

        peak = max(slots, key=lambda s: (use_pct(s), s.flights), default=None)
        tight = sorted(
            (s for s in slots if use_pct(s) > SAFE_USE * 100 and s is not peak),
            key=lambda s: -use_pct(s),
        )
        # Merge overlapping 15-min slots into readable periods.
        tight_periods: list[list[int]] = []
        for s in sorted(tight, key=lambda s: s.start):
            if tight_periods and s.start <= tight_periods[-1][1]:
                tight_periods[-1][1] = max(tight_periods[-1][1], s.start + _SLOT_MIN - 1)
                tight_periods[-1][2] = max(tight_periods[-1][2], use_pct(s))
            else:
                tight_periods.append([s.start, s.start + _SLOT_MIN - 1, use_pct(s)])
        tight_text = ", ".join(
            f"{_hm(a)}–{_hm(b)} ({u}%)" for a, b, u in tight_periods[:6]
        )

        # Must add: slots with more flights than people.
        must_add = _add_until(slots, heads, pool_shifts, 1.0)
        # Comfortable: every slot <= SAFE_USE.
        comfy_add = _add_until(slots, heads, pool_shifts, SAFE_USE)
        comfortable = assigned + sum(comfy_add.values())

        pool_squeezed = [m for m in squeezed_min if lo <= m < hi]

        removable: dict[str, int] = {}
        if not must_add and not pool_squeezed:
            # Check slots across the whole day: a pool's shift also
            # covers neighbouring hours.
            removable = _removable(
                all_slots, heads, pool_shifts, max(volume_floor, p2f_floor),
            )
        n_remove = sum(removable.values())
        n_add = sum(must_add.values())
        n_add = max(n_add, max(volume_floor, p2f_floor) - assigned)

        deltas: dict[str, int] = {s: 0 for s in pool_shifts}
        if n_add > 0:
            for s, k in must_add.items():
                deltas[s] += k
            extra = n_add - sum(must_add.values())
            if extra > 0:
                deltas[lead] += extra
            action = f"ADD {n_add}"
            required = assigned + n_add
        elif n_remove >= _REMOVE_MIN:
            for s, k in removable.items():
                deltas[s] -= k
            action = f"REMOVE {n_remove}"
            required = assigned - n_remove
        else:
            action = "OK"
            required = assigned

        target_per_shift = {s: assigned_per_shift[s] + deltas[s] for s in pool_shifts}
        parts = []
        for s in pool_shifts:
            if deltas[s] > 0:
                parts.append(f"ADD {deltas[s]} to {s}")
            elif deltas[s] < 0:
                parts.append(f"REMOVE {-deltas[s]} from {s}")
        breakdown_action = " · ".join(parts) if parts else "no change"

        if n_add > 0 and sum(must_add.values()):
            bottleneck = "peak"
        elif n_add > 0:
            bottleneck = "volume"
        elif peak is not None and use_pct(peak) > SAFE_USE * 100:
            bottleneck = "peak"
        else:
            bottleneck = "volume" if n_remove < _REMOVE_MIN else "none"

        # Plain-language reason, for the dashboard card.
        if peak is None:
            explanation = "No flights in this pool's hours."
        elif n_add > 0 and sum(must_add.values()):
            explanation = (
                f"At {_slot_label(peak.start)} {peak.flights} flights depart but "
                f"only {peak.people(heads)} people can take them — some flights "
                "must be squeezed (spacing relaxed) unless people are added."
            )
        elif n_add > 0:
            explanation = (
                f"{len(in_band)} flights need about {volume_floor} people at the "
                f"per-person target ({staff_target}); {assigned} are rostered."
            )
        elif action.startswith("REMOVE"):
            explanation = (
                f"Even without {n_remove} people, every 15-min slot stays at or "
                f"below {round(SAFE_USE * 100)}% of the people who can take it, "
                "and total volume still fits the targets."
            )
        else:
            spare_by_volume = assigned - max(volume_floor, p2f_floor)
            lead_in = (
                f"Total volume alone would need only {volume_floor}, but "
                if spare_by_volume >= _REMOVE_MIN else ""
            )
            at = "at" if lead_in else "At"
            explanation = (
                f"{lead_in}{at} {_slot_label(peak.start)} {peak.flights} flights "
                f"depart with {peak.people(heads)} people able to take them "
                f"({use_pct(peak)}%). Removing people would squeeze flights there."
                if use_pct(peak) > SAFE_USE * 100 else
                f"Busiest 15 min ({_slot_label(peak.start)}) uses "
                f"{use_pct(peak)}% of the people who can take those flights."
            )
        if pool_squeezed:
            explanation += (
                f" The last Allocate had to squeeze {len(pool_squeezed)} "
                f"flight(s) here ({', '.join(_hm(m) for m in pool_squeezed[:5])})."
            )

        out.append(StaffingRecommendation(
            pool=pool,
            shifts_in_pool=" + ".join(pool_shifts),
            assigned_today=assigned,
            assigned_breakdown=", ".join(f"{s}={assigned_per_shift[s]}" for s in pool_shifts),
            target_breakdown=", ".join(f"{s}={target_per_shift[s]}" for s in pool_shifts),
            breakdown_action=breakdown_action,
            flights_in_window=len(in_band),
            peak_window=_slot_label(peak.start) if peak else "",
            peak_window_flights=peak.flights if peak else 0,
            peak_window_people=peak.people(heads) if peak else 0,
            peak_use_pct=use_pct(peak) if peak else 0,
            tight_windows=tight_text,
            has_p2f_flight=has_p2f,
            target_per_staff=staff_target,
            hard_cap_per_staff=hard_cap,
            volume_floor=volume_floor,
            p2f_floor=p2f_floor,
            required=required,
            comfortable=comfortable,
            bottleneck=bottleneck,
            gap=required - assigned,
            action=action,
            squeezed=len(pool_squeezed),
            squeezed_times=", ".join(_hm(m) for m in pool_squeezed),
            explanation=explanation,
            implied_avg_per_staff=round(len(in_band) / required, 1) if required else 0.0,
        ))
    return out


# ---------- state glue ----------


def run_for_state(state: AppState, d_day: date_t) -> list[dict]:
    """Compute the recommendation from current state, store it on
    ``state.staffing`` and return the JSON-friendly rows. After an
    Allocate, flights it had to squeeze (W230) feed in as evidence."""
    flights = [
        (r.date, r.std, r.ops_class.value) for r in state.allocatable_cleaned()
    ]
    assigned: dict[str, int] = {s: 0 for s in _SHIFTS}
    zcs: dict[str, int] = {s: 0 for s in _SHIFTS}
    d_iso = d_day.isoformat()
    for av in state.availability:
        if av.date.isoformat() != d_iso or not av.assignable:
            continue
        if av.role is Role.AM:
            continue
        if av.current_shift in assigned:
            assigned[av.current_shift] += 1
            if av.role is Role.ZC:
                zcs[av.current_shift] += 1

    squeezed = [
        (r.date, r.std) for r in state.allocations
        if "FORCED:" in (r.warning or "")
        and "no rule relaxed" not in (r.warning or "")
    ]
    rows = compute_staffing(
        flights, assigned, d_day, zc_by_shift=zcs, squeezed=squeezed,
    )
    out = [asdict(r) for r in rows]
    with state.lock:
        state.staffing = out
    return out
