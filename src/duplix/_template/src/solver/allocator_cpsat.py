"""CP-SAT model for Step 4 — flight allocation.

Hard constraints (always enforced):
  H1   exactly one staff per flight       — Σ x[f, e] = 1
  H10  15-min spacing per staff           — optional IntervalVar +
                                            AddNoOverlap
  H16  per-staff hard caps                — Σ_f x[f, e] ≤ cap[e]

Soft objectives (opt-in via flags, added one at a time across E6.X):
  S1   asymmetric per-staff count balance — penalty around the preferred
                                            target: below = 100/short,
                                            +1 above = 50, +2 above = 200,
                                            +3 above = 800 (most groups
                                            cap at +2 per H16 anyway)
  S3   heavy-flight spread                 — added in E6.2
  S4   pairing stability tie-breaker       — added in E6.3

Hard constraints NOT encoded here (handled elsewhere):
  H2-H5, H9, H11-H13, H15  — eligibility matrix (E3) shapes the
                              decision variable space; only legal
                              (flight, staff) pairs become x vars
  H6, H7, H8               — pair generator (E4) + post-solve assembly
                              (E7); the solver never sees pairs
  H14                      — Step 3's responsibility (24-hr rule)

Decision variables: ``x[(flight_id, employee_id)] ∈ {0, 1}``, sparse
over the eligibility matrix. With ~2000 flights × ~95 staff but only
~15-25 eligible staff per flight, the variable count is ~40-60k rather
than ~190k. CP-SAT handles this comfortably under a 60s time budget on
the sample day per D5.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_t
from typing import Any

from ortools.sat.python import cp_model

from ..allocator.caps import hard_cap_for, preferred_target_for
from ..allocator.eligibility import EligibilityContext, p2f_partial_block_anchors
from ..allocator.p2f_priority import select_p2f_priority
from ..allocator.windows import (
    HANDOVER_WINDOW_MIN,
    P2F_WINDOW_HALF_MIN,
    SHIFT_NOMINAL_MIN,
    SPACING_HARD_MIN,
    ZC_BUFFER_END_MIN,
    ZC_BUFFER_START_MIN,
    awkward_eligible_shifts,
    is_in_h10_relaxed_band,
    shift_boundary_within_30min,
    spacing_floor_min_for_flight,
    std_to_ops_day_minutes,
)
from ..schemas import FlightInput, OpsClass, Role, StaffMember

# S1 — symmetric distance-to-day-level (Finding 1, 2026-05-15).
# The prior asymmetric ladder (below × 100, +1 × 50, +2 × 550, +3 × 2050)
# pulled the solver toward a static "preferred" (typically 22). The
# manual allocator instead picks ONE level per day from flight volume
# and pins almost every non-handler staff to it. This single-weight
# symmetric form makes the solver behave the same way.
#
# Weight 200 is between the old +1-above (50) and +2-above (550) —
# strong enough to dominate the small tiebreakers (S3 × 5, S4 × 1,
# S5/S6 × 10, S7 × 15) but well below S_OUTER (5000) and the
# unallocated-penalty (100k).
S1_WEIGHT_DAY_LEVEL_DISTANCE = 200
# 2026-05-24: escalating below-target boosts. Above-target stays flat
# at 200 (don't punish people for being slightly over preferred load),
# but going way below target gets sharply more expensive so the
# solver fights harder to redistribute load to under-utilised staff.
# Triggered by the user observing 2-3 STAFF at 14-15 flights while
# others were at 24. Tier thresholds picked so 0-2 below stays cheap
# (small variance OK), 3-5 below moderate, 6+ below heavy.
#   below_dev    = max(0, level - actual)
#   below_tier2  = max(0, below_dev - 2)   # 3rd, 4th, 5th below
#   below_tier3  = max(0, below_dev - 5)   # 6th and beyond
S1_WEIGHT_BELOW_TIER2_BOOST = 300
S1_WEIGHT_BELOW_TIER3_BOOST = 500
# Legacy ladder kept as commented constants for reference / revert.
# S1_WEIGHT_BELOW_PER_FLIGHT = 100
# S1_WEIGHT_AT_LEAST_1_ABOVE = 50
# S1_WEIGHT_AT_LEAST_2_ABOVE = 500
# S1_WEIGHT_AT_LEAST_3_ABOVE = 1500

# S1b per-bucket spread minimization (added 2026-05-10; bumped 2026-05-28).
# For each (shift, role) bucket of non-handler staff, add soft penalty on
# (bucket_max - bucket_min). Weight bumped 400 -> 600 (2026-05-28) after
# real-world solves showed -19 / -7 / -5 outliers in otherwise healthy
# shifts: the solver was choosing to leave under-loaded staff rather than
# pay the S1b cost to equalize. Higher weight makes equalization more
# aggressive per-second of solve time.
# Example: spread=1 costs +600, spread=2 costs +1200, spread=3 costs +1800.
# Dominates S1 marginal costs (e.g. 22+24 = 50+550 = 600 + spread=2 1200 = 1800
# vs 23+23 = 50+50 = 100 + spread=0 = 100 — 18x preference for tight).
S1B_WEIGHT_BUCKET_SPREAD = 600

# S3 heavy-flight spread (prompt §soft_objectives + REF_Constraints).
# A flight with PAX >= S3_HEAVY_THRESHOLD counts as "heavy". The
# objective penalizes the maximum heavy-count across all staff, so
# piling heavies on one person costs more than distributing them.
# Weight 5 is mild — it should sway ties only, never override S1.
S3_HEAVY_THRESHOLD_PAX = 200
S3_WEIGHT_MAX_HEAVY = 5

# S4 pairing stability tie-breaker (briefing + REF_Constraints).
# Adds a tiny per-(flight, staff) cost proportional to the staff's
# position in the input list. Caller passes ``staff`` in IN_Staff row
# order; lower-row staff get lower S4 cost, so the solver prefers them
# in ties — making solutions deterministic without affecting balance
# (weight 1, dwarfed by S1 at 50+ and S3 at 5).
S4_WEIGHT_PER_ROW = 1

# S5 handover-window distribution preference (round-3 update).
# In the (nominal_end, nominal_end + HANDOVER_WINDOW_MIN] overlap window,
# both prev and next shift are eligible. S5 tilts the solver: assigning
# such a flight to the next shift costs S5_WEIGHT_PER_HANDOVER_FLIGHT
# more than the prev shift, so the prev shift absorbs it unless S1
# count balance prefers otherwise. Weight 10 sits between S3 (5) and
# S1 (50+) — actively biases distribution but never overrides count.
S5_WEIGHT_HANDOVER_NEXT_SHIFT = 10

# S6 awkward-window routing preference (round-3 update).
# Replaces H15 hard routing. For each awkward STD, the awkward_eligible
# table names ONE preferred shift; assigning to that shift costs 0,
# assigning to any other shift on duty during the window costs
# S6_WEIGHT_AWKWARD_NON_PREFERRED. Weight 10 is mild — moves ties.
S6_WEIGHT_AWKWARD_NON_PREFERRED = 10

# S7 ZC report buffer avoidance (round-3 update).
# Replaces H9 hard exclusion. ZC assignments inside their report
# buffer cost S7_WEIGHT_ZC_BUFFER per flight; non-buffer ZC slots
# are free. Weight 15 is higher than S6/S5 because the buffers exist
# for a real workflow reason (report writing), but lower than S1
# count-balance — the solver may push ZCs into buffers if their
# count target can't otherwise be met.
S7_WEIGHT_ZC_BUFFER = 15

# S_outer A/A1 outer-band penalty (user direction 2026-05-12, §1).
# A staff's STD window has an inner band (hard) and an outer band
# (soft). Outer-band assignments are allowed only when no inner
# placement is feasible. Weight is high enough to dominate S1+S1b
# but low enough that an unallocated-flight penalty (100k) still
# wins — i.e., better to use the outer band than leave a flight
# unallocated.
S_OUTER_BAND_PENALTY = 5_000

# ZC workload floor (2026-09-22 fix, second pass).
# H18 used to enforce band["min"] (e.g. N/ZC = 14) as a HARD per-staff
# floor. That's fine when the day's flight supply can support it, but
# a static config number has no idea whether it's actually achievable
# — a zero-night-ops day (or any day where H10 spacing eats into the
# raw eligible count) can make band_lo mathematically impossible,
# which drags the ENTIRE model to INFEASIBLE, not just that bucket.
# This happened in production 2026-09-22 (Night ops = 0, N/ZC floor
# forced to 14 -> 2117/2117 flights unallocated).
#
# Fix: make it a SOFT shortfall penalty instead of a hard constraint.
# Weight is high — just under S_OUTER — so the solver still fights
# hard to reach the floor whenever it's reachable, but a genuinely
# unreachable floor costs points instead of blowing up the solve.
ZC_FLOOR_SHORTFALL_WEIGHT = 4_000

# P2F priority (2026-09-22, user direction): a P2F flight is worth 10x an
# ordinary flight when it is left unallocated, so whenever a handler's
# cap / spacing forces a choice, the normal flight is the one that moves
# to someone else. Normal flights stay at 100_000 (``unassigned_penalty``
# in ``solve_allocation``). P2F flights that ``select_p2f_priority`` can
# reserve are fixed outright; this weight covers the rest.
P2F_UNASSIGNED_PENALTY = 1_000_000

# Phase 4 (2026-05-14, INTL overhaul, Change 5) — INTL spacing policy.
# Hard floor: same-handler INTL DEP→INTL DEP pairs with gap < 15 min
# are forbidden. The constant below is the SPEC anchor; enforcement
# is via H10 (SPACING_HARD_MIN=15) which already forbids any same-
# staff flight pair within 15 min — the INTL case is a subset.
H_INTL_SPACING_HARD_MIN = 15

# Soft penalty for same-handler INTL DEP pairs in the 15-30 min band:
#   - heavy band [15, 20] min → S_INTL_SPACING_HEAVY (extra buffer needed)
#   - light band [21, 29] min → S_INTL_SPACING_LIGHT (prefer ≥30 min)
# Sized between S1 marginals (50, 550) and S_outer (5000): heavy at 800
# is comparable to S1's +2-above marginal; light at 100 is a tie-breaker.
S_INTL_SPACING_HEAVY = 800
S_INTL_SPACING_LIGHT = 100
S_INTL_SPACING_PENALTY = S_INTL_SPACING_HEAVY  # alias for the spec test

# Spacing policy applies to ALL shifts (amendment 2026-05-14 afternoon:
# N is no longer exempt — the issue is most acute on Night).
S_INTL_SPACING_SHIFTS: frozenset[str] = frozenset({"M", "M1", "A", "A1", "N"})

# Phase 4 / Change 6 — fair INTL distribution within each shift.
# Penalize the (max - min) of INTL count per handler within each shift
# bucket. Weight is modest (mild tiebreaker, dominated by S1 + spacing)
# so the solver only spreads when it can do so cheaply.
S_INTL_FAIR_PENALTY = 50
S_INTL_FAIR_SHIFTS: frozenset[str] = frozenset({"M", "M1", "A", "A1", "N"})

# Patch 2026-05-15 — band-targeted "prefer stable shift" preference.
# Within the H10-relaxed bands, the solver should prefer staff whose
# shift is NOT in transition (nominal start or end within ±30 min of
# the flight's STD). Implemented as a soft penalty: each (flight,
# staff) pair where the flight is in a relaxed band AND the staff's
# shift has a nearby boundary pays this cost.
#
# Weight 200 sits above S1-marginal (+1 above = 50, +2 above = 500
# marginal) — strong enough to bias picks but won't dominate the
# 100k UNASSIGNED penalty. Dominated by S_OUTER (5000) so a soft
# outer-band assignment isn't traded against shift stability.
S_BAND_SHIFT_STABILITY_PENALTY = 200


@dataclass(frozen=True)
class AllocationSolverResult:
    """Output of solve_allocation_hard_only."""

    status: str
    """One of OPTIMAL | FEASIBLE | INFEASIBLE | TIMEOUT | MODEL_INVALID."""

    assignments: dict[str, str]
    """flight_id -> employee_id. Empty when status is INFEASIBLE / TIMEOUT
    without any feasible solution found."""

    wall_clock_seconds: float


# Status codes are CpSolverStatus enum values at runtime; we accept Any
# for the dict key type because the ortools stubs don't expose
# CpSolverStatus as a public type alias.
_STATUS_MAP: dict[Any, str] = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.UNKNOWN: "TIMEOUT",
}


def _build_s1_term(
    model: cp_model.CpModel,
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
    day_level_by_bucket: dict[tuple[str, Role], int] | None = None,
) -> Any:
    """S1 — per-staff distance from the day's workload level.

    2026-05-24 rewrite: ASYMMETRIC with escalating below-target boosts.

    Above target stays flat at S1_WEIGHT_DAY_LEVEL_DISTANCE = 200 per
    unit (don't penalise minor over-utilisation). Below target uses
    the same base 200 plus two tier boosts that kick in for deeper
    deficits, so the solver fights harder against the "2-3 STAFF
    stuck at 14 while others are at 24" pattern that the prior
    symmetric formula tolerated.

    Per-staff penalty:

        above_dev    = max(0, actual - level)
        below_dev    = max(0, level - actual)
        below_tier2  = max(0, below_dev - 2)       # 3rd, 4th, 5th below
        below_tier3  = max(0, below_dev - 5)       # 6th and beyond
        penalty_e    = 200·above_dev
                     + 200·below_dev
                     + 300·below_tier2
                     + 500·below_tier3

    Cost gradient at the deep-deficit end is ~5× the old symmetric
    weight, with no change for 0-2 below or any above-target case.

    Fallback: when ``day_level_by_bucket`` is None or doesn't contain
    a bucket, that staff is excluded from S1 (cost 0). Avoids breaking
    callers that don't pass the new arg yet.
    """
    if not day_level_by_bucket:
        return 0
    terms: list[Any] = []
    for s in staff:
        if s.shift_today is None or s.role == Role.AM:
            continue
        my_vars = [
            x[(f.unique_id, s.employee_id)]
            for f in flights if (f.unique_id, s.employee_id) in x
        ]
        if not my_vars:
            continue
        level = day_level_by_bucket.get((s.shift_today, s.role))
        if level is None:
            continue
        actual_expr = sum(my_vars)
        # above_dev = max(0, actual - level)
        # below_dev = max(0, level - actual)
        # Lower bound 0 is implicit (NonNegativeIntegerVar).
        above_dev = model.new_int_var(0, 30, f"s1_above_{s.employee_id}")
        below_dev = model.new_int_var(0, 30, f"s1_below_{s.employee_id}")
        model.add(above_dev >= actual_expr - level)
        model.add(below_dev >= level - actual_expr)
        # Tier boosts that kick in only when below_dev exceeds the
        # threshold. The lower-bound = 0 on the int_var combined with
        # the inequality gives max(0, below_dev - k).
        below_tier2 = model.new_int_var(0, 30, f"s1_btier2_{s.employee_id}")
        model.add(below_tier2 >= below_dev - 2)
        below_tier3 = model.new_int_var(0, 30, f"s1_btier3_{s.employee_id}")
        model.add(below_tier3 >= below_dev - 5)
        terms.append(
            S1_WEIGHT_DAY_LEVEL_DISTANCE * above_dev
            + S1_WEIGHT_DAY_LEVEL_DISTANCE * below_dev
            + S1_WEIGHT_BELOW_TIER2_BOOST * below_tier2
            + S1_WEIGHT_BELOW_TIER3_BOOST * below_tier3
        )
    return sum(terms) if terms else 0


def _build_s3_term(
    model: cp_model.CpModel,
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
) -> Any:
    """S3 heavy-flight spread. Penalize the MAX heavy-count across
    staff, so piling all PAX≥200 flights on one person is more
    expensive than spreading them. Weight is mild (5) — sways ties
    only.
    """
    heavy_flights = [f for f in flights if f.load >= S3_HEAVY_THRESHOLD_PAX]
    if not heavy_flights:
        return 0
    max_heavy = model.new_int_var(0, len(heavy_flights), "s3_max_heavy")
    for s in staff:
        if s.shift_today is None or s.role == Role.AM:
            continue
        my_heavies = [
            x[(f.unique_id, s.employee_id)] for f in heavy_flights
            if (f.unique_id, s.employee_id) in x
        ]
        if my_heavies:
            model.add(max_heavy >= sum(my_heavies))
    return S3_WEIGHT_MAX_HEAVY * max_heavy


def _build_s4_term(
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
) -> Any:
    """S4 pairing stability — make solutions deterministic by adding
    a tiny per-assignment cost proportional to the staff's position
    in the (row-ordered) input list. Solver prefers lower-row staff
    in ties; weight 1 ensures S4 never overrides S1 or S3.

    Caller is responsible for passing ``staff`` in IN_Staff row order.
    """
    terms: list[Any] = []
    for idx, s in enumerate(staff):
        if s.shift_today is None or s.role == Role.AM:
            continue
        my_vars = [
            x[(f.unique_id, s.employee_id)] for f in flights
            if (f.unique_id, s.employee_id) in x
        ]
        if my_vars and idx > 0:
            terms.append(S4_WEIGHT_PER_ROW * idx * sum(my_vars))
    return sum(terms) if terms else 0


def _build_s5_handover_term(
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
    ops_day: date_t,
) -> Any:
    """S5 handover-window distribution preference (round-3).

    For each flight whose STD falls in the (nominal_end, nominal_end +
    HANDOVER_WINDOW_MIN] handover overlap of some shift S, assigning to
    a NEXT-shift staff (e.g., A handler when STD is in M's handover) is
    penalized; assigning to the prev shift (M handler) is free. This
    creates a default of "handover stays with prev shift unless S1
    count balance prefers otherwise."
    """
    terms: list[Any] = []
    for f in flights:
        std_min = std_to_ops_day_minutes(f.std, f.date, ops_day)
        # Identify all shifts whose handover-overlap window contains
        # this STD. (A flight in the M→A handover overlap is in BOTH M's
        # tail AND A's nominal — we want to penalize the A side.)
        for shift, (_, nominal_end) in SHIFT_NOMINAL_MIN.items():
            if nominal_end < std_min <= nominal_end + HANDOVER_WINDOW_MIN:
                # `shift` is the prev shift. Penalize assignments to any
                # staff whose shift is NOT `shift` — they're the "next
                # shift" for this STD.
                for s in staff:
                    if s.shift_today is None or s.role == Role.AM:
                        continue
                    if s.shift_today == shift:
                        continue
                    if (f.unique_id, s.employee_id) in x:
                        terms.append(
                            S5_WEIGHT_HANDOVER_NEXT_SHIFT
                            * x[(f.unique_id, s.employee_id)]
                        )
    return sum(terms) if terms else 0


def _build_s6_awkward_term(
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
    ops_day: date_t,
) -> Any:
    """S6 awkward-window routing preference (round-3).

    Replaces H15 hard routing. For each flight in an awkward window,
    assignments to non-preferred shifts cost extra. The "preferred" shift
    is the first entry in the awkward_eligible_shifts tuple.
    """
    terms: list[Any] = []
    for f in flights:
        awk = (
            f.awkward_eligible_shifts if f.is_awkward_window else
            awkward_eligible_shifts(f.std, f.date, ops_day)
        )
        if not awk:
            continue
        preferred_shift = awk[0]
        for s in staff:
            if s.shift_today is None or s.role == Role.AM:
                continue
            if s.shift_today == preferred_shift:
                continue
            if (f.unique_id, s.employee_id) in x:
                terms.append(
                    S6_WEIGHT_AWKWARD_NON_PREFERRED * x[(f.unique_id, s.employee_id)]
                )
    return sum(terms) if terms else 0


def _build_s7_zc_buffer_term(
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
    ops_day: date_t,
) -> Any:
    """S7 ZC report-buffer avoidance (round-3).

    Replaces H9 hard exclusion. ZC assignments inside their report
    buffers cost S7 per flight; non-buffer ZC assignments are free.
    """
    terms: list[Any] = []
    for s in staff:
        if s.role != Role.ZC or s.shift_today is None:
            continue
        b1_start, b1_end = ZC_BUFFER_START_MIN[s.shift_today]
        b2_start, b2_end = ZC_BUFFER_END_MIN[s.shift_today]
        for f in flights:
            if (f.unique_id, s.employee_id) not in x:
                continue
            std_min = std_to_ops_day_minutes(f.std, f.date, ops_day)
            in_buffer = (
                (b1_start <= std_min < b1_end)
                or (b2_start <= std_min < b2_end)
            )
            if in_buffer:
                terms.append(
                    S7_WEIGHT_ZC_BUFFER * x[(f.unique_id, s.employee_id)]
                )
    return sum(terms) if terms else 0


def _build_outer_band_term(
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
    ops_day: date_t,
) -> Any:
    """S_outer A/A1 outer-band penalty (user 2026-05-12, §1).

    For every (flight, staff) pair where the flight's STD lies in the
    staff's shift's OUTER band (allowed but penalized), pay
    ``S_OUTER_BAND_PENALTY`` per assignment. Inner-band assignments are
    free. Only A and A1 have outer bands; M / M1 / N return False from
    ``is_in_std_outer_band`` so they contribute zero terms.
    """
    from ..allocator.windows import is_in_std_outer_band
    terms: list[Any] = []
    for s in staff:
        if s.shift_today is None or s.role == Role.AM:
            continue
        if s.shift_today not in ("A", "A1"):
            continue
        for f in flights:
            if (f.unique_id, s.employee_id) not in x:
                continue
            if is_in_std_outer_band(f.std, f.date, ops_day, s.shift_today):
                terms.append(
                    S_OUTER_BAND_PENALTY * x[(f.unique_id, s.employee_id)]
                )
    return sum(terms) if terms else 0


def _build_s_intl_spacing_term(
    model: cp_model.CpModel,
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
    eligibility: dict[str, set[str]],
    ops_day: date_t,
) -> Any:
    """Phase 4 / Change 5 — soft penalty for same-handler INTL DEP pairs
    in the 15-30 min spacing band.

    Mechanics:
      For every pair (i, j) of INTL DEP flights with 15 ≤ |std_i - std_j|
      < 30 min and every staff e eligible for BOTH, introduce a boolean
      ``both_e_ij`` that equals AND(x[i,e], x[j,e]). Add
      ``weight * both_e_ij`` to the returned objective term.

      ``weight`` is ``S_INTL_SPACING_HEAVY`` when gap ∈ [15, 20]
      (the "tight" band), else ``S_INTL_SPACING_LIGHT``.

    Gaps < 15 are already forbidden by H10 (SPACING_HARD_MIN).
    Gaps ≥ 30 are the preferred state and receive no penalty.
    Staff on shifts outside ``S_INTL_SPACING_SHIFTS`` are skipped — though
    today the set covers every shift, so this is a no-op filter.
    """
    intl_flights = [f for f in flights if f.is_international]
    if len(intl_flights) < 2:
        return 0
    intl_flights.sort(
        key=lambda f: std_to_ops_day_minutes(f.std, f.date, ops_day)
    )
    staff_by_id = {s.employee_id: s for s in staff}
    terms: list[Any] = []
    n = len(intl_flights)
    for i in range(n):
        fi = intl_flights[i]
        std_i = std_to_ops_day_minutes(fi.std, fi.date, ops_day)
        elig_i = eligibility.get(fi.unique_id, set())
        for j in range(i + 1, n):
            fj = intl_flights[j]
            std_j = std_to_ops_day_minutes(fj.std, fj.date, ops_day)
            gap = std_j - std_i
            if gap < H_INTL_SPACING_HARD_MIN:
                continue  # H10 forbids; no soft term needed
            if gap >= 30:
                break  # subsequent j's are even further out — done
            weight = (
                S_INTL_SPACING_HEAVY if gap <= 20 else S_INTL_SPACING_LIGHT
            )
            elig_j = eligibility.get(fj.unique_id, set())
            common = elig_i & elig_j
            for eid in common:
                s = staff_by_id.get(eid)
                if s is None or s.shift_today not in S_INTL_SPACING_SHIFTS:
                    continue
                xi = x.get((fi.unique_id, eid))
                xj = x.get((fj.unique_id, eid))
                if xi is None or xj is None:
                    continue
                both = model.new_bool_var(
                    f"intl_both_{fi.unique_id}_{fj.unique_id}_{eid}"
                )
                # both == xi AND xj
                model.add(both <= xi)
                model.add(both <= xj)
                model.add(both >= xi + xj - 1)
                terms.append(weight * both)
    if not terms:
        return None
    return sum(terms)


def _build_s_intl_fair_term(
    model: cp_model.CpModel,
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
) -> Any:
    """Phase 4 / Change 6 — soft penalty on INTL DEP distribution variance
    within each shift.

    For each shift in ``S_INTL_FAIR_SHIFTS``, compute ``intl_max - intl_min``
    across all non-handler staff on that shift; weight by
    ``S_INTL_FAIR_PENALTY`` and add to the objective.

    Uses the same max/min-IntVar pattern as the H18 spread mechanism — cheap
    O(shifts) constraints, no quadratic blowup.
    """
    intl_flight_ids = {f.unique_id for f in flights if f.is_international}
    if not intl_flight_ids:
        return 0
    # Bucket staff by shift (skip AM, skip off-day, skip handlers' bucket
    # mixing — handlers are kept in their own bucket to avoid pulling the
    # min down with a P2F/NORSE who only does INTL incidentally).
    bucket_by_shift: dict[str, list[StaffMember]] = defaultdict(list)
    for s in staff:
        if s.shift_today is None or s.role == Role.AM:
            continue
        if s.shift_today not in S_INTL_FAIR_SHIFTS:
            continue
        bucket_by_shift[s.shift_today].append(s)

    terms: list[Any] = []
    for shift_code, bucket in bucket_by_shift.items():
        if len(bucket) < 2:
            continue
        # intl count per staff in this shift; range [0, len(intl)].
        upper = len(intl_flight_ids)
        intl_min = model.new_int_var(
            0, upper, f"intl_min_{shift_code}"
        )
        intl_max = model.new_int_var(
            0, upper, f"intl_max_{shift_code}"
        )
        n_pinned = 0
        for s in bucket:
            intl_vars = [
                x[(fid, s.employee_id)] for fid in intl_flight_ids
                if (fid, s.employee_id) in x
            ]
            if not intl_vars:
                continue
            actual = sum(intl_vars)
            model.add(actual >= intl_min)
            model.add(actual <= intl_max)
            n_pinned += 1
        if n_pinned >= 2:
            terms.append(S_INTL_FAIR_PENALTY * (intl_max - intl_min))
    if not terms:
        return None
    return sum(terms)


def _build_s_band_shift_stability_term(
    flights: list[FlightInput],
    staff: list[StaffMember],
    x: dict[tuple[str, str], Any],
    ops_day: date_t,
) -> Any:
    """Patch 2026-05-15 — band-targeted "prefer stable shift" penalty.

    For every (flight, staff) pair where:
      * the flight's STD lies in an H10-relaxed band
        (``windows.H10_RELAXED_BANDS_MIN``), AND
      * the staff's shift has its nominal start OR end within ±30 min
        of the flight's STD,
    add a soft penalty of ``S_BAND_SHIFT_STABILITY_PENALTY``.

    Effect: during rush bands (when H10 is relaxed to 10 min), the
    solver prefers handlers whose shift is mid-stride — not just
    starting or ending. Staff in handover are still eligible, just
    chosen second.

    Pure tiebreaker — weight 200 is well below the unallocated cost
    (100 000) and the outer-band penalty (5 000), so the solver
    won't leave a flight unallocated to avoid the boundary case.
    """
    terms: list[Any] = []
    for f in flights:
        if not is_in_h10_relaxed_band(f.std, f.date, ops_day):
            continue
        for s in staff:
            if s.shift_today is None or s.role == Role.AM:
                continue
            if (f.unique_id, s.employee_id) not in x:
                continue
            if shift_boundary_within_30min(
                s.shift_today, f.std, f.date, ops_day,
            ):
                terms.append(
                    S_BAND_SHIFT_STABILITY_PENALTY
                    * x[(f.unique_id, s.employee_id)]
                )
    if not terms:
        return None
    return sum(terms)


def _add_p2f_partial_block_constraints(
    model: cp_model.CpModel,
    flights: list[FlightInput],
    x: dict[tuple[str, str], Any],
    ctx: EligibilityContext,
) -> None:
    """H4 partial windows: max 1 normal flight in the ±15 min windows
    around (P2F_STD - 1hr) and (P2F_STD + 20min) for each P2F handler.
    The full hard-block window (D-3hrs) is already filtered by F4 in
    eligibility; this function adds the soft-blocked max-1 constraints.

    2026-05-26 (user direction): ZC+P2F combined handlers (roster cell
    `M/P2F/ZC` etc.) are skipped here. Their ZC cap is already low
    (14-15 day / 10-11 night) and applies to regular+P2F combined;
    stacking the partial buffer windows on top removes too many flights
    and they end up well below cap. The hard D-3hrs block in F6 still
    applies — that's briefing time and can't be compressed.
    """
    for handler_id, p2f_anchors in ctx.p2f_flight_minutes_by_handler.items():
        if not p2f_anchors:
            continue
        if handler_id in ctx.zc_p2f_handler_ids:
            continue
        before_centers, after_centers = p2f_partial_block_anchors(p2f_anchors)
        for centers in (before_centers, after_centers):
            for center in centers:
                window_vars = []
                for f in flights:
                    if f.ops_class == OpsClass.P2F:
                        continue
                    if (f.unique_id, handler_id) not in x:
                        continue
                    std_min = std_to_ops_day_minutes(
                        f.std, f.date, ctx.ops_day,
                    )
                    if abs(std_min - center) <= P2F_WINDOW_HALF_MIN:
                        window_vars.append(x[(f.unique_id, handler_id)])
                if window_vars:
                    model.add(sum(window_vars) <= 1)


def solve_allocation(
    flights: list[FlightInput],
    staff: list[StaffMember],
    eligibility: dict[str, set[str]],
    *,
    ops_day: date_t,
    max_seconds: int = 60,
    enable_s1_count_balance: bool = False,
    enable_s3_heavy_spread: bool = False,
    enable_s4_pair_stability: bool = False,
    enable_s5_handover_preference: bool = False,
    enable_s6_awkward_preference: bool = False,
    enable_s7_zc_buffer_avoidance: bool = False,
    enable_s_intl_spacing: bool = False,
    enable_s_intl_fair: bool = False,
    enable_s_band_shift_stability: bool = False,
    elig_ctx: EligibilityContext | None = None,
    solution_hints: dict[str, str] | None = None,
    waive_h10_triples: frozenset[tuple[str, str, str]] = frozenset(),
    raise_cap_uids: frozenset[tuple[str, str]] = frozenset(),
    day_level_by_bucket: dict[tuple[str, Role], int] | None = None,
    pinned_assignments: dict[str, str] | None = None,
) -> AllocationSolverResult:
    """Solve the allocation problem.

    With all soft-objective flags False (default), the solver returns
    the first feasible assignment that satisfies H1, H10, H16, and (if
    ``elig_ctx`` provides P2F handler info) the H4 partial-block
    constraints. With soft flags enabled, it minimizes the chosen
    weighted sum.

    Pass ``elig_ctx`` (the same one used to build the eligibility
    matrix) so the solver can apply the H4 partial-block max-1
    constraints around the handler's D-1hr and D+20min windows.

    Caller is responsible for:
      - filtering flights to today's ops scope (D + N tail to 05:00 D+1)
      - filtering staff to assignable today (off-day staff dropped)
      - building the eligibility matrix via allocator/eligibility.py
      - aborting with W201 if any flight has empty eligibility (this
        function will return INFEASIBLE in that case as a safety net)
    """
    model = cp_model.CpModel()
    staff_by_id = {s.employee_id: s for s in staff}

    # ---- Decision variables + per-staff flight lists ----
    x: dict[tuple[str, str], cp_model.IntVar] = {}
    # Phase R: switched from intervals_by_staff (used with add_no_overlap)
    # to flights_by_staff (used to build per-pair H10 constraints below).
    # The pairwise formulation lets us skip exactly the pairs the operator
    # waived via an override of type=waive_h10_pair, without compromising H10
    # against the staff's other flights.
    flights_by_staff: dict[str, list[FlightInput]] = defaultdict(list)
    flight_by_uid: dict[str, FlightInput] = {f.unique_id: f for f in flights}

    for flight in flights:
        eligible_ids = eligibility.get(flight.unique_id, set())
        for emp_id in eligible_ids:
            if emp_id not in staff_by_id:
                # Defensive: eligibility names a staff we don't have.
                # Skip rather than crash — caller's bug, surface as
                # missing eligibility downstream.
                continue
            v = model.new_bool_var(f"x_{flight.unique_id}_{emp_id}")
            x[(flight.unique_id, emp_id)] = v
            flights_by_staff[emp_id].append(flight)

    # ---- H1 (relaxed): at most one staff per flight ----
    # Hard constraint: a flight cannot be assigned to two staff.
    # Soft preference: every flight SHOULD be assigned, but if aggregate
    # capacity is short, some flights are left unallocated rather than
    # the whole problem becoming infeasible. The objective rewards
    # assignment via _UNASSIGNED_PENALTY below.
    unassigned_indicators: list[Any] = []
    p2f_unassigned_indicators: list[Any] = []
    for flight in flights:
        eligible_ids = eligibility.get(flight.unique_id, set())
        present = [
            x[(flight.unique_id, eid)] for eid in eligible_ids
            if (flight.unique_id, eid) in x
        ]
        if not present:
            # Treat as unconditionally unassigned (matrix had no eligible
            # staff). The orchestrator should already have logged W201;
            # we just need a sentinel so the count is right.
            const_one = model.new_constant(1)
            unassigned_indicators.append(const_one)
        else:
            model.add(sum(present) <= 1)
            # 1 - sum(present) is 1 when the flight is unassigned,
            # 0 otherwise. We sum these into the objective.
            ind = model.new_bool_var(f"unassigned_{flight.unique_id}")
            model.add(ind == 1 - sum(present))
            # P2F flights carry a heavier penalty (see
            # P2F_UNASSIGNED_PENALTY) so they win any tie against a
            # normal flight for the handler's cap / spacing.
            if flight.ops_class == OpsClass.P2F:
                p2f_unassigned_indicators.append(ind)
            else:
                unassigned_indicators.append(ind)

    # ---- H10: per-staff same-flight spacing, band-aware + waiver-aware ----
    # Replaces the prior model.add_no_overlap formulation. For every pair
    # of flights eligible to the same staff with gap < required spacing
    # (10 in relaxed bands when both flights are in a band, else 15),
    # add an at-most-one constraint UNLESS the operator waived this pair
    # via an override of type=waive_h10_pair.
    from ..allocator.windows import required_spacing_min as _required_spacing
    from ..schemas import OpsClass as _OC_h10
    n_h10_pairs = 0
    n_h10_waived = 0
    n_h10_norse_skipped = 0
    for emp_id, emp_flights in flights_by_staff.items():
        if len(emp_flights) < 2:
            continue
        # Sort by STD so we iterate close-in-time pairs first.
        with_min = sorted(
            (
                (std_to_ops_day_minutes(f.std, f.date, ops_day), f)
                for f in emp_flights
            ),
            key=lambda t: t[0],
        )
        for i in range(len(with_min)):
            std_i, fi = with_min[i]
            for j in range(i + 1, len(with_min)):
                std_j, fj = with_min[j]
                gap = std_j - std_i
                floor = _required_spacing(std_i, std_j)
                if gap >= floor:
                    # Pairs from here on are even further apart (sorted).
                    break
                # 2026-05-24 (user direction): NORSE flights skip H10
                # entirely. The NORSE handler doesn't physically
                # operate the flight — they batch-email pax loads for
                # all NORSE legs together in a few minutes. So one
                # handler can hold any number of NORSE flights AND
                # also take regular flights right alongside them —
                # there's no physical handover that needs the 15-min
                # gap. Skip H10 if EITHER side of the pair is NORSE.
                if (fi.ops_class == _OC_h10.NORSE
                        or fj.ops_class == _OC_h10.NORSE):
                    n_h10_norse_skipped += 1
                    continue
                # Phase R waiver check. Order-insensitive — both
                # (uid_a, uid_b, emp) and (uid_b, uid_a, emp) accepted.
                if (
                    (fi.unique_id, fj.unique_id, emp_id) in waive_h10_triples
                    or (fj.unique_id, fi.unique_id, emp_id) in waive_h10_triples
                ):
                    n_h10_waived += 1
                    continue
                model.add(
                    x[(fi.unique_id, emp_id)] + x[(fj.unique_id, emp_id)] <= 1
                )
                n_h10_pairs += 1
    if n_h10_norse_skipped:
        # Plain ASCII to survive Windows cp1252 console encoding
        # (Unicode arrows like ↔ raise UnicodeEncodeError on the
        # default stdout encoder).
        print(f"  H10: skipped {n_h10_norse_skipped} NORSE-NORSE pair(s) (no physical handover)")
    if waive_h10_triples:
        print(
            f"  H10 pairwise: {n_h10_pairs} forbidden, "
            f"{n_h10_waived} waived via override"
        )

    from ..schemas import OpsClass as _OpsClass

    # ---- H18: tight workload spread per shift/role bucket ----
    # Per user direction 2026-05-10: people on the same shift+role
    # should get the same number of flights "as much as possible".
    # We add a HARD constraint: within each (shift, role) bucket,
    # max(actual) - min(actual) <= 2. Handlers (P2F + NORSE) are
    # EXCLUDED — their workload mix differs (handler flights count
    # differently per the cap rules) and they'd otherwise drag the
    # min down and force everyone else under their target.
    p2f_handler_set_for_spread: set[str] = set()
    norse_handler_set_for_spread: set[str] = set()
    if elig_ctx is not None:
        p2f_handler_set_for_spread = set(elig_ctx.p2f_handler_by_shift.values())
        norse_handler_set_for_spread = set(elig_ctx.norse_handler_ids)
    excluded_handlers = p2f_handler_set_for_spread | norse_handler_set_for_spread
    spread_groups: dict[tuple[str, Role], list[StaffMember]] = defaultdict(list)
    for s in staff:
        if s.shift_today is None or s.role == Role.AM:
            continue
        if s.employee_id in excluded_handlers:
            continue
        spread_groups[(s.shift_today, s.role)].append(s)
    # Per (shift, role) bucket, pin every assigned staff's count to
    # [target - 1, target + 1] so spread is at most 2. Direct integer
    # bounds — no IntVar middlemen — so the constraint is unambiguous.
    # Plus a soft spread-min term (bucket_max - bucket_min) to push the
    # solver toward spread=1 or 0 within that hard range.
    # Handlers excluded (their workload mix is different).
    #
    # 2026-05-18 (sick re-solve): when ``pinned_assignments`` is
    # active, BYPASS H18 entirely. The pin already locks every staff
    # to their prior count; H18 bounds (recomputed against a possibly
    # different day_level after the sick removal) routinely conflict
    # with the pins and make the model infeasible. Pin is the source
    # of truth in this mode.
    n_h18_buckets = 0
    spread_obj_terms: list[Any] = []
    skip_h18 = bool(pinned_assignments)
    if skip_h18:
        print(
            f"  H18 bypassed: pinned-assignments mode (sick re-solve) — "
            f"pins are the bucket bounds"
        )
    for (shift_code, role), bucket in spread_groups.items():
        if skip_h18:
            continue
        if len(bucket) < 2:
            # 2026-05-16 (user direction): single-staff buckets — most
            # commonly a lone ZC on a shift (e.g. KULDEEP SINGH at N/ZC)
            # — were previously unconstrained. Without H18, the soft
            # S1 penalty alone was too weak to pull them toward target
            # and they ended at 5/11. Now: still no bucket-spread
            # (nothing to spread over), but we DO add a hard
            # actual >= band.min on the staff. Only for ZC role —
            # STAFF singletons rarely happen and have larger pools
            # to draw from. CP-SAT will report INFEASIBLE if the lone
            # ZC genuinely doesn't have band.min eligible flights;
            # that's the right loud-failure signal.
            if role == Role.ZC and bucket:
                from ..allocator.caps import get_band
                band = get_band(shift_code, role)
                if band is not None:
                    s = bucket[0]
                    my_vars = [
                        x[(f.unique_id, s.employee_id)] for f in flights
                        if (f.unique_id, s.employee_id) in x
                    ]
                    if my_vars:
                        # 2026-09-22 fix (2nd pass): SOFT floor, not
                        # hard. A hard `model.add(sum >= band["min"])`
                        # can make the WHOLE model INFEASIBLE whenever
                        # the day's supply (or H10 spacing exclusions
                        # among ``my_vars``) can't actually reach
                        # band["min"] — a raw eligible-flight count
                        # isn't proof that count is simultaneously
                        # selectable. Shortfall costs points instead.
                        shortfall = model.new_int_var(
                            0, band["min"],
                            f"zc_floor_short_{s.employee_id}",
                        )
                        model.add(shortfall >= band["min"] - sum(my_vars))
                        spread_obj_terms.append(
                            ZC_FLOOR_SHORTFALL_WEIGHT * shortfall
                        )
                        print(
                            f"  H18 single-ZC soft floor: "
                            f"{shift_code}/{role.value} ({s.name}) "
                            f"target >= {band['min']} (soft)"
                        )
            continue
        sample_staff = bucket[0]
        target = preferred_target_for(sample_staff)
        cap = hard_cap_for(sample_staff)
        # Finding 1 (2026-05-15): hard UPPER bound now slides with the
        # day's saturation level. On heavy days (level == cap) the
        # bucket can reach cap directly; on lighter days the bound
        # stays close to the level so the solver can't over-pack a
        # single staff when the day's volume doesn't demand it. Falls
        # back to the static target+1 when day_level_by_bucket isn't
        # passed (e.g. tests).
        day_level = (
            day_level_by_bucket.get((shift_code, role))
            if day_level_by_bucket else None
        )
        if day_level is not None:
            hi = min(day_level + 1, cap)
        else:
            hi = min(target + 1, cap)
        # 2026-05-16 (user direction): ZC buckets also get a hard
        # lower bound = band.min. Without it the solver was leaving
        # ZCs at near-zero while STAFF saturated at target. STAFF
        # buckets stay at lo=0 because their pools are large and the
        # S1 day-level penalty is sufficient there.
        if role == Role.ZC:
            from ..allocator.caps import get_band
            band = get_band(shift_code, role)
            band_lo = band["min"] if band else 0
        else:
            band_lo = 0
        # bucket_min / bucket_max IntVars track the bucket's distribution
        # and feed the soft S1b spread-min penalty added to objective.
        # 2026-09-22 fix: domain floor is always 0 here, NOT band_lo.
        # band_lo is enforced per-staff below, clamped to that staff's
        # own eligible-flight supply — if the IntVar's own domain
        # floor were band_lo, a supply-starved staff (actual_expr forced
        # below band_lo) would still be constrained via
        # ``actual_expr >= bucket_min >= band_lo``, silently
        # reintroducing the same infeasibility this fix removes.
        bucket_min = model.new_int_var(
            0, hi, f"bmin_{shift_code}_{role.value}",
        )
        bucket_max = model.new_int_var(
            0, hi, f"bmax_{shift_code}_{role.value}",
        )
        n_constrained = 0
        for s in bucket:
            emp_vars = [
                x[(f.unique_id, s.employee_id)] for f in flights
                if (f.unique_id, s.employee_id) in x
            ]
            if not emp_vars:
                continue
            actual_expr = sum(emp_vars)
            model.add(actual_expr <= hi)
            model.add(actual_expr >= bucket_min)
            model.add(actual_expr <= bucket_max)
            if band_lo > 0:
                # 2026-09-22 fix (2nd pass): SOFT floor, not hard. See
                # ZC_FLOOR_SHORTFALL_WEIGHT comment — a hard floor here
                # is what caused the 2026-09-22 all-flights-unallocated
                # incident (Night ops = 0, N/ZC floor = 14 -> INFEASIBLE).
                # shortfall = max(0, band_lo - actual_expr); penalized,
                # never blocks the solve.
                shortfall = model.new_int_var(
                    0, band_lo, f"zc_floor_short_{s.employee_id}",
                )
                model.add(shortfall >= band_lo - actual_expr)
                spread_obj_terms.append(
                    ZC_FLOOR_SHORTFALL_WEIGHT * shortfall
                )
            n_constrained += 1
        if n_constrained >= 2:
            spread_obj_terms.append(
                S1B_WEIGHT_BUCKET_SPREAD * (bucket_max - bucket_min)
            )
        n_h18_buckets += 1
        floor_note = f" (soft floor={band_lo})" if band_lo > 0 else ""
        print(f"  H18 spread bucket {shift_code}/{role.value}: "
              f"{len(bucket)} staff ({n_constrained} pinned), "
              f"target={target}, range=[0, {hi}]{floor_note}")
    print(f"  H18 total buckets pinned: {n_h18_buckets}")

    # ---- H19 removed 2026-05-11 ----
    # Per user direction 2026-05-11: TEST and FERRY flights are now
    # distributed as normal flights and counted under workload — no
    # per-staff cap separate from H16. CHARTER (B-type) is also a
    # normal flight. The H16 cap is the only ceiling.

    # ---- H17: P2F-per-handler cap of 8 ----
    # Per user direction 2026-05-10: no single P2F handler should be
    # asked to do more than 8 P2F flights in a day — that's the
    # operational ceiling. If a shift has more than 8 P2F flights,
    # the assigner should nominate an additional handler for that
    # shift (W215 warning surfaces this need).
    p2f_cap_per_handler = 8
    if elig_ctx is not None:
        p2f_handler_set_for_cap = set(
            elig_ctx.p2f_handler_by_shift.values()
        )
        for emp_id in p2f_handler_set_for_cap:
            p2f_vars = [
                x[(f.unique_id, emp_id)]
                for f in flights
                if f.ops_class == _OpsClass.P2F
                and (f.unique_id, emp_id) in x
            ]
            if p2f_vars:
                model.add(sum(p2f_vars) <= p2f_cap_per_handler)

    # ---- H16: per-staff hard caps ----
    # Per user direction 2026-05-10 (clarified):
    #   P2F flights DO count against the P2F handler's cap. A P2F
    #     handler's total work (P2F + regular) is capped at the peer
    #     limit (ZC=16, STAFF=24, etc.) — this naturally keeps them
    #     at or below the workload of non-handler peers since their
    #     P2F flights eat into the cap.
    #   NORSE flights are EXEMPT from the NORSE handler's cap. NORSE
    #     work goes on top of the regular cap.
    norse_handler_set: set[str] = set()
    if elig_ctx is not None:
        norse_handler_set = set(elig_ctx.norse_handler_ids)

    for emp_id in flights_by_staff:
        cap = hard_cap_for(staff_by_id[emp_id])
        is_norse_handler = emp_id in norse_handler_set
        # Phase R: ``raise_cap_uids`` lets the operator nominate one
        # specific (flight, staff) pair to land OUTSIDE the H16 cap.
        # Effectively "this flight is on top of cap" — same semantic as
        # NORSE exemption for the NORSE handler.
        emp_vars = [
            x[(f.unique_id, emp_id)]
            for f in flights
            if (f.unique_id, emp_id) in x
            and not (is_norse_handler and f.ops_class == _OpsClass.NORSE)
            and (f.unique_id, emp_id) not in raise_cap_uids
        ]
        if emp_vars:
            model.add(sum(emp_vars) <= cap)

    # ---- H4 partial blocks (P2F handler ±15 min around D-1hr / D+20min) ----
    if elig_ctx is not None:
        _add_p2f_partial_block_constraints(model, flights, x, elig_ctx)

    # ---- P2F first: reserve each handler's P2F flights ----
    # 2026-09-22 (user direction): the nominated P2F handler must get his
    # P2F flights BEFORE any normal flight is considered. Previously a P2F
    # flight was just another flight with the same unallocated cost, so a
    # handler could be filled with normal flights (cap H16 / spacing H10)
    # and have P2F left over. ``select_p2f_priority`` returns only the
    # P2F flights that can be fixed without conflicting with H17 / H16 /
    # H10 / existing pins, so this cannot make the model infeasible.
    # Normal flights are then solved around them; any P2F flight not
    # reserved still gets the heavier P2F_UNASSIGNED_PENALTY above.
    if elig_ctx is not None and elig_ctx.p2f_handler_by_shift:
        reserved_p2f, skipped_p2f = select_p2f_priority(
            flights,
            eligibility,
            set(elig_ctx.p2f_handler_by_shift.values()),
            ops_day=ops_day,
            cap_by_staff={
                eid: hard_cap_for(s) for eid, s in staff_by_id.items()
            },
            pinned_assignments=pinned_assignments,
            waive_h10_triples=waive_h10_triples,
        )
        n_p2f_reserved = 0
        for _uid, _eid in reserved_p2f.items():
            _var = x.get((_uid, _eid))
            if _var is None:
                continue
            model.add(_var == 1)
            n_p2f_reserved += 1
        if n_p2f_reserved or skipped_p2f:
            print(
                f"  P2F-first: {n_p2f_reserved} P2F flight(s) reserved for "
                f"their handlers; {len(skipped_p2f)} left to the solver"
            )
            for _uid, _why in skipped_p2f:
                print(f"    P2F {_uid} not reserved: {_why}")

    # ---- Soft objectives ----
    # Unassigned-flight penalty dominates everything else, so the solver
    # always prefers to allocate over leaving idle. Weight = 100_000 so a
    # single un-assigned flight is more costly than every other soft
    # term combined at typical problem sizes.
    unassigned_penalty = 100_000
    obj_terms: list[Any] = []
    if unassigned_indicators:
        obj_terms.append(unassigned_penalty * sum(unassigned_indicators))
    if p2f_unassigned_indicators:
        obj_terms.append(
            P2F_UNASSIGNED_PENALTY * sum(p2f_unassigned_indicators)
        )
    # S1b bucket spread minimization — wired alongside S1 count balance.
    # Always on when S1 is enabled (they're complementary: S1 pins each
    # staff near their preferred target, S1b pushes the whole bucket toward
    # uniform).
    if enable_s1_count_balance and spread_obj_terms:
        obj_terms.append(sum(spread_obj_terms))
    if enable_s1_count_balance:
        obj_terms.append(
            _build_s1_term(model, flights, staff, x, day_level_by_bucket)
        )
    if enable_s3_heavy_spread:
        obj_terms.append(_build_s3_term(model, flights, staff, x))
    if enable_s4_pair_stability:
        obj_terms.append(_build_s4_term(flights, staff, x))
    if enable_s5_handover_preference:
        obj_terms.append(_build_s5_handover_term(flights, staff, x, ops_day))
    if enable_s6_awkward_preference:
        obj_terms.append(_build_s6_awkward_term(flights, staff, x, ops_day))
    if enable_s7_zc_buffer_avoidance:
        obj_terms.append(_build_s7_zc_buffer_term(flights, staff, x, ops_day))
    # Phase 4 / Changes 5 + 6 (2026-05-14, INTL overhaul).
    # The helpers return ``None`` when there's nothing to penalize; a
    # CP-SAT LinearExpr otherwise. CP-SAT LinearExpr raises on bool() and
    # on equality with int, so test with ``is not None`` only.
    if enable_s_intl_spacing:
        spacing_term = _build_s_intl_spacing_term(
            model, flights, staff, x, eligibility, ops_day,
        )
        if spacing_term is not None:
            obj_terms.append(spacing_term)
    if enable_s_intl_fair:
        fair_term = _build_s_intl_fair_term(model, flights, staff, x)
        if fair_term is not None:
            obj_terms.append(fair_term)
    # Patch 2026-05-15 — band-targeted "prefer stable shift" within
    # the H10-relaxed bands.
    if enable_s_band_shift_stability:
        stability_term = _build_s_band_shift_stability_term(
            flights, staff, x, ops_day,
        )
        if stability_term is not None:
            obj_terms.append(stability_term)
    # S_outer is always on (no flag) — penalizes A/A1 outer-band STDs
    # so the solver only uses them when the inner band can't absorb
    # the flight. Required by user direction 2026-05-12 §1.
    obj_terms.append(_build_outer_band_term(flights, staff, x, ops_day))
    if obj_terms:
        model.minimize(sum(obj_terms))

    # ---- Pinned assignments (2026-05-18 — sick-call re-solve) ----
    # Pinned pairs become HARD constraints (model.add x == 1) — every
    # other staff for the same flight is implicitly forced to 0 by H1.
    # Used by the sick-call iteration flow in step3: prior allocation
    # is locked in place; only the sick staff's flights have free
    # decision variables, so the solver redistributes ONLY those.
    #
    # Pins that can't be applied (flight or staff disappeared between
    # runs) are silently skipped; the solver is otherwise free for
    # those rows. Returns to a cold solve when ``pinned_assignments``
    # is None or empty.
    n_pinned_applied = 0
    n_pinned_skipped = 0
    if pinned_assignments:
        for fid, eid in pinned_assignments.items():
            target = x.get((fid, eid))
            if target is None:
                n_pinned_skipped += 1
                continue
            model.add(target == 1)
            n_pinned_applied += 1
        if n_pinned_applied or n_pinned_skipped:
            print(
                f"  solver: pinned {n_pinned_applied} prior assignment(s); "
                f"{n_pinned_skipped} skipped (no longer eligible)"
            )

    # ---- Warm-start hints (Phase R — recommender re-solve speedup) ----
    # When ``solution_hints`` is supplied, mark each (flight, staff) pair
    # in it with x=1 and every other variable for that flight with x=0.
    # CP-SAT uses the hint as the search's starting point — if the hint
    # is still feasible under the (possibly-changed) constraints, the
    # solver typically returns in 30-60 % of the cold-start time.
    #
    # Hints are SUGGESTIONS, not constraints. If a hint pair is no longer
    # eligible (e.g. an override removed that staff's slot), the solver
    # silently ignores that hint and searches normally.
    n_hints_applied = 0
    if solution_hints:
        for fid, eid in solution_hints.items():
            target = x.get((fid, eid))
            if target is None:
                continue
            model.add_hint(target, 1)
            n_hints_applied += 1
        if n_hints_applied:
            print(
                f"  solver: warm-start with {n_hints_applied} solution "
                f"hints (of {len(solution_hints)} requested)"
            )

    # ---- Solve ----
    # Per user direction 2026-05-11: time budget removed — solver runs
    # to completion (OPTIMAL or proven INFEASIBLE). max_seconds = 0 or
    # negative disables the time bound; positive values still cap.
    solver = cp_model.CpSolver()
    if max_seconds and max_seconds > 0:
        solver.parameters.max_time_in_seconds = float(max_seconds)
    # 2026-05-24: keep stochastic search by design. A previous attempt
    # to pin random_seed=42 backfired — user pointed out: if 42 happens
    # to be unlucky for a given problem, every re-run produces the same
    # bad result and "re-allocate to get a better answer" stops being
    # an escape hatch. We rely on CP-SAT's default randomness so the
    # operator can simply re-run when a result looks thin.
    solver.parameters.stop_after_first_solution = False
    # 2026-05-28 (user direction — slow re-solve fix): when warm-start
    # hints are present in meaningful quantity, the prior allocation is
    # a tight upper bound on the objective. Without an early-exit
    # signal, CP-SAT runs to max_seconds polishing the objective even
    # though the warm-start already provided a near-optimal solution.
    # A relative gap limit tells the solver to stop when proving better
    # would be operationally pointless.
    #
    # Two-tier behavior:
    #   * n_hints_applied >= 50% of total flights → "re-solve mode":
    #     cap wall-clock at 200s AND accept 5% gap. Sick / recommender
    #     re-solves complete in 30-60s typical (vs 700s cold) because
    #     CP-SAT proves the warm-started solution near-optimal fast.
    #   * Fewer hints → "cold-solve mode": full max_seconds, full proof.
    if n_hints_applied >= max(1, len(flights) // 2):
        solver.parameters.relative_gap_limit = 0.05
        prev_max = solver.parameters.max_time_in_seconds
        if prev_max <= 0 or prev_max > 200.0:
            solver.parameters.max_time_in_seconds = 200.0
        print(
            f"  solver: re-solve mode — warm-start has {n_hints_applied} "
            f"hints of {len(flights)} flights; capped at 200s + 5% gap "
            f"(was {prev_max:.0f}s full budget)"
        )
    import time as _stime
    _solve_t0 = _stime.monotonic()
    status = solver.solve(model)
    _solve_elapsed = _stime.monotonic() - _solve_t0
    print(f"  solver: status={_STATUS_MAP.get(status, 'UNKNOWN')}, elapsed={_solve_elapsed:.1f}s")
    status_str = _STATUS_MAP.get(status, "UNKNOWN")

    assignments: dict[str, str] = {}
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for (fid, eid), v in x.items():
            if solver.value(v) == 1:
                # No flight should ever have two staff (H1), but assert
                # to surface bugs in the constraint encoding early.
                assert fid not in assignments, (
                    f"H1 violated: {fid} assigned to both "
                    f"{assignments[fid]} and {eid}"
                )
                assignments[fid] = eid

    # H1 is now a soft preference, so a feasible solution may legitimately
    # leave some flights unassigned (the UNASSIGNED_PENALTY objective
    # term makes the solver minimize this). No post-solve assertion.

    return AllocationSolverResult(
        status=status_str,
        assignments=assignments,
        wall_clock_seconds=solver.wall_time,
    )


def solve_allocation_hard_only(
    flights: list[FlightInput],
    staff: list[StaffMember],
    eligibility: dict[str, set[str]],
    *,
    ops_day: date_t,
    max_seconds: int = 60,
) -> AllocationSolverResult:
    """E5 entry point — hard constraints only, no soft objective.

    Thin wrapper around solve_allocation with all soft-objective flags
    False. Kept for tests that assert pure constraint behavior in
    isolation.
    """
    return solve_allocation(
        flights, staff, eligibility,
        ops_day=ops_day, max_seconds=max_seconds,
    )
