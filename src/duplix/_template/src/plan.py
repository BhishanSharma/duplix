"""Plan stage — Step 1 + Step 2 only, plus the summary the assigner
reviews before allocating.

Flow:

  1. Step 1: clean the uploaded flight schedule into ``state.cleaned``.
  2. Step 2: extract the rosters into ``state.availability``.
  3. Compute the summary (this module's contribution):
        - flights to be planned, per ops_class and per shift window
        - staff on shift today, per shift
        - recommendations: day/night headcount, P2F handlers (1 per 8
          P2F flights per shift), NORSE handlers (1 per 10 NORSE)
        - the gap, if any
  4. Store it on ``state.plan_summary`` / ``state.plan_text`` and print
     a readable version to the server console.

The assigner reviews the dashboard, stages any overrides in the drawer,
then clicks Allocate.
"""

from __future__ import annotations

from collections import Counter
from datetime import date as date_t
from pathlib import Path

from .allocator.windows import SHIFT_NOMINAL_MIN, std_to_ops_day_minutes
from .config import load_config
from .schemas import OpsClass
from .state import AppState
from .step1_clean_flights import run as run_step1
from .step2_extract_roster import run as run_step2

# Operational ceilings used for recommendations.
P2F_FLIGHTS_PER_HANDLER = 8
NORSE_FLIGHTS_PER_HANDLER = 10
TARGET_FLIGHTS_PER_STAFF_DAY = 22  # STAFF day-shift acceptable
TARGET_FLIGHTS_PER_STAFF_NIGHT = 19  # STAFF night-shift acceptable


def _cleaned_breakdown(state: AppState) -> dict[str, int]:
    """Flight count per ops_class. Every class is present (0 when empty)
    so the dashboard cards never have to guard for missing keys."""
    out = {o.value: 0 for o in OpsClass}
    for ops, rows in state.cleaned.items():
        out[ops.value] = len(rows)
    return out


def _p2f_per_shift(state: AppState, d_day: date_t) -> dict[str, int]:
    """Count P2F flights by which M/A/N shift's nominal window holds them."""
    counts: Counter[str] = Counter()
    for row in state.cleaned.get(OpsClass.P2F, []):
        std_min = std_to_ops_day_minutes(row.std, row.date, d_day)
        for shift, (start, end) in SHIFT_NOMINAL_MIN.items():
            if shift in ("M", "A", "N") and start <= std_min <= end:
                counts[shift] += 1
                break
    return dict(counts)


def _staff_counts_today(state: AppState, d_day: date_t) -> dict[str, int]:
    """Count assignable staff per shift on D."""
    counts: Counter[str] = Counter()
    d_iso = d_day.isoformat()
    for av in state.availability:
        if av.date.isoformat() != d_iso or not av.assignable:
            continue
        if not av.current_shift:
            continue
        counts[av.current_shift] += 1
    return dict(counts)


def _format_summary(
    flights: dict[str, int],
    p2f_per_shift: dict[str, int],
    staff: dict[str, int],
    d_day: date_t,
) -> tuple[str, dict[str, object]]:
    """Build the plain-text summary and a JSON-friendly dict for the UI."""
    target_per_day_staff = TARGET_FLIGHTS_PER_STAFF_DAY
    target_per_night_staff = TARGET_FLIGHTS_PER_STAFF_NIGHT
    day_staff = sum(staff.get(s, 0) for s in ("M", "A", "M1", "A1"))
    night_staff = staff.get("N", 0)

    def _ceil_div(a: int, b: int) -> int:
        return -(-a // b) if b else 0

    needed_day_staff = _ceil_div(flights["day"], target_per_day_staff)
    needed_night_staff = _ceil_div(flights["night"], target_per_night_staff)
    day_gap = max(0, needed_day_staff - day_staff)
    night_gap = max(0, needed_night_staff - night_staff)

    p2f_handlers_needed = sum(
        _ceil_div(n, P2F_FLIGHTS_PER_HANDLER) for n in p2f_per_shift.values()
    )
    norse_handlers_needed = _ceil_div(flights["norse"], NORSE_FLIGHTS_PER_HANDLER)

    lines: list[str] = []
    lines.append(f"=== Plan summary for D={d_day.isoformat()} ===\n")
    lines.append("FLIGHTS TO BE PLANNED")
    lines.append(f"  Day ops:  {flights['day']:5d}")
    lines.append(f"  Night ops:{flights['night']:5d}")
    lines.append(f"  P2F:      {flights['p2f']:5d}     "
                 f"per shift: M={p2f_per_shift.get('M', 0)} "
                 f"A={p2f_per_shift.get('A', 0)} "
                 f"N={p2f_per_shift.get('N', 0)}")
    lines.append(f"  NORSE:    {flights['norse']:5d}")
    lines.append(f"  Ferry:    {flights['ferry']:5d}")
    lines.append(f"  Charter:  {flights['charter']:5d}")
    lines.append(f"  Test:     {flights['test']:5d}")
    # GULF is extract-only — the solver never sees it, so it must not
    # inflate the plannable total.
    plannable_total = sum(v for k, v in flights.items() if k != "gulf")
    lines.append(f"  Total:    {plannable_total:5d}")
    lines.append(f"  Gulf (extracted, NOT allocated): {flights['gulf']}")
    lines.append("")
    lines.append("STAFF ON SHIFT TODAY")
    for s in ("M", "A", "N", "M1", "A1"):
        lines.append(f"  {s:3} : {staff.get(s, 0):3d}")
    lines.append(f"  Day staff (M+A+M1+A1):  {day_staff}")
    lines.append(f"  Night staff (N):         {night_staff}")
    lines.append("")
    lines.append("RECOMMENDATIONS")
    lines.append(
        f"  Day staff needed (target {target_per_day_staff}/staff): "
        f"{needed_day_staff}  -> gap: {day_gap}"
    )
    lines.append(
        f"  Night staff needed (target {target_per_night_staff}/staff): "
        f"{needed_night_staff}  -> gap: {night_gap}"
    )
    lines.append(
        f"  P2F handlers needed (1 per {P2F_FLIGHTS_PER_HANDLER} flights): "
        f"{p2f_handlers_needed}"
    )
    lines.append(
        f"  NORSE handlers needed (1 per {NORSE_FLIGHTS_PER_HANDLER} flights): "
        f"{norse_handlers_needed}"
    )
    lines.append("")
    lines.append("NEXT STEP")
    if day_gap or night_gap:
        lines.append("  Add staff for the gap shifts via the Override drawer,")
        lines.append("  OR proceed to allocate (gap flights will be UNALLOCATED).")
    else:
        lines.append("  Staff coverage looks OK. Click Allocate.")

    text = "\n".join(lines)
    payload = {
        "date": d_day.isoformat(),
        "flights": flights,
        "p2f_per_shift": p2f_per_shift,
        "staff": staff,
        "day_staff_total": day_staff,
        "night_staff_total": night_staff,
        "needed_day_staff": needed_day_staff,
        "needed_night_staff": needed_night_staff,
        "day_gap": day_gap,
        "night_gap": night_gap,
        "p2f_handlers_needed": p2f_handlers_needed,
        "norse_handlers_needed": norse_handlers_needed,
    }
    return text, payload


def run(
    state: AppState,
    d_day: date_t,
    config_path: Path | str = "configs/config.yml",
) -> dict[str, object]:
    """Run Step 1 + Step 2, then build the plan summary. Returns the
    JSON-friendly payload (also stored on ``state.plan_summary``)."""
    load_config(config_path)  # fail fast if the config is unreadable
    run_step1(state, d_day, config_path)
    run_step2(state, d_day, config_path)

    flights = _cleaned_breakdown(state)
    p2f_per_shift = _p2f_per_shift(state, d_day)
    staff = _staff_counts_today(state, d_day)
    text, payload = _format_summary(flights, p2f_per_shift, staff, d_day)
    print(text)

    with state.lock:
        state.run_date = d_day
        state.plan_summary = payload
        state.plan_text = text

    # Staffing recommender: required = max(peak_floor, volume_floor,
    # p2f_floor) per shift. Advisory — a failure here must not break Plan.
    from . import recommender_staffing as _rs
    try:
        _rs.run_for_state(state, d_day)
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: staffing recommender failed: {exc}")

    state.stamp_run(mode="Plan", solver_status="(Plan only — no solve)",
                    duration_s=None)
    return payload
