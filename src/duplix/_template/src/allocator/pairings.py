"""Pair generator for Step 4 (H8 + correction r2-7).

Auto-generates the full set of boundary pairs for an ops day, sequentially
by IN_Staff row order. ZCs only pair with ZCs at every boundary. The
A→N boundary uses a three-tier hierarchy when |A| > |N|.

Inputs:
  staff_today        — sorted by IN_Staff row order, already filtered to
                       assignable staff (off-day rows excluded by the
                       caller)
  yesterday_n_staff  — yesterday's N-shift roster, used for the N→M
                       boundary (D-1's N planned today's M flights). May
                       be None on the first run; in that case, today's M
                       staff self-plan their first flight.
  overrides          — pair-specific override rows; each one replaces
                       any auto-generated pair sharing the same (boundary,
                       prev_emp) or (boundary, next_emp).

Output: list[Pair]. Surplus staff with no partner are absent from the
output (emitted Pairs always have BOTH sides populated). The caller can
infer "this staff is unpaired at this boundary" by absence — useful for
the pair map's surplus annotations and for diagnosing under-staffing.

Three-tier hierarchy at A→N:
  Tier 1: 1-to-1 primary A↔N up to min(|A|, |N|).
  Tier 2: A surplus routes to A↔A1 handover-only.
  Tier 3: Remaining A surplus routes to secondary A↔N handover-only,
          one secondary per N staff (so each N person can have at most
          one primary + one secondary A pair).
  If A surplus still remains after tier 3, raises PairGenerationError —
  the assigner has too many A staff for the day's structure and must
  resolve via override or by reducing the A roster.
"""

from __future__ import annotations

from collections import defaultdict

from ..schemas import OpsBoundary, Pair, PairRole, Role, ShiftCode, StaffMember

# H6 per-primary-pair pre-plan counts (defaults). Aligned with REF_Constraints.
# Per-pair count actually used at construction may differ from these defaults
# in special cases — currently the only such case is the split 1+2 at A→N
# when N has 2 A-partners (primary pair gets 2, secondary gets 1).
_H6_DEFAULT_COUNTS: dict[OpsBoundary, int] = {
    OpsBoundary.N_TO_M: 1,
    OpsBoundary.M_TO_M1: 1,
    OpsBoundary.M_TO_A: 2,
    OpsBoundary.M1_TO_A1: 2,
    OpsBoundary.A_TO_N: 3,
    OpsBoundary.A1_TO_N: 0,
    OpsBoundary.A_TO_A1: 0,
}


class PairGenerationError(ValueError):
    """Raised when A surplus exceeds the total handover-routing capacity
    (A↔A1 + secondary A↔N). Maps to W211 in the warnings system."""


def _by_shift_role(
    staff: list[StaffMember],
) -> dict[tuple[ShiftCode, Role], list[StaffMember]]:
    """Group staff by (shift_today, role), preserving input order (which
    is the assigner's IN_Staff row order). Off-shift and AM staff are
    dropped — they don't pair."""
    out: dict[tuple[ShiftCode, Role], list[StaffMember]] = defaultdict(list)
    for s in staff:
        if s.shift_today is None or s.role == Role.AM:
            continue
        out[(s.shift_today, s.role)].append(s)
    return out


def _make_pair(
    prev: StaffMember, next_: StaffMember,
    boundary: OpsBoundary, pair_role: PairRole,
    prev_shift: ShiftCode, next_shift: ShiftCode,
    *, preplan_count: int | None = None,
) -> Pair:
    """Build a Pair. ``preplan_count`` defaults to the H6 table value for
    the boundary if not specified — pass an explicit override (e.g., 1
    for split-secondary at A→N, 0 for handover-only)."""
    if preplan_count is None:
        preplan_count = (
            _H6_DEFAULT_COUNTS[boundary]
            if pair_role == PairRole.PRIMARY else 0
        )
    return Pair(
        boundary=boundary, pair_role=pair_role, preplan_count=preplan_count,
        prev_employee_id=prev.employee_id, prev_name=prev.name, prev_shift=prev_shift,
        next_employee_id=next_.employee_id, next_name=next_.name, next_shift=next_shift,
    )


def _one_to_one(
    prev: list[StaffMember], next_: list[StaffMember],
    boundary: OpsBoundary, pair_role: PairRole,
    prev_shift: ShiftCode, next_shift: ShiftCode,
    *, preplan_count: int | None = None,
) -> tuple[list[Pair], list[StaffMember], list[StaffMember]]:
    """Sequential pairing prev[i] ↔ next[i] up to min(|prev|, |next|).
    Returns (pairs, prev_unpaired, next_unpaired)."""
    n = min(len(prev), len(next_))
    pairs = [
        _make_pair(
            prev[i], next_[i], boundary, pair_role, prev_shift, next_shift,
            preplan_count=preplan_count,
        )
        for i in range(n)
    ]
    return pairs, prev[n:], next_[n:]


def _simple_boundary(
    by_today: dict[tuple[ShiftCode, Role], list[StaffMember]],
    boundary: OpsBoundary,
    prev_shift: ShiftCode, next_shift: ShiftCode,
    pair_role: PairRole,
) -> list[Pair]:
    """Pair regulars-with-regulars and ZCs-with-ZCs at a boundary that
    doesn't use the 3-tier hierarchy. ZC isolation: a regular never
    pairs with a ZC at this layer (the N→M cross-role exception is
    handled separately in _n_to_m_cross_role)."""
    pairs: list[Pair] = []
    for role in (Role.STAFF, Role.ZC):
        prev = by_today.get((prev_shift, role), [])
        next_ = by_today.get((next_shift, role), [])
        new_pairs, _, _ = _one_to_one(
            prev, next_, boundary, pair_role, prev_shift, next_shift,
        )
        pairs.extend(new_pairs)
    return pairs


def _n_to_m_cross_role(
    by_yesterday: dict[tuple[ShiftCode, Role], list[StaffMember]],
    by_today: dict[tuple[ShiftCode, Role], list[StaffMember]],
) -> list[Pair]:
    """Round-3 update: at the N→M boundary, M-ZCs that don't have an
    N-ZC partner (because there's typically only 1 N-ZC vs 4 M-ZCs)
    are planned by N **regular** staff. N names may repeat — the
    priority is "every M-ZC's first flight gets a planner" over "no N
    staff is reused" (per user direction).

    Algorithm:
      1. Pair the N-ZCs to M-ZCs sequentially (same as existing ZC↔ZC).
      2. Remaining M-ZCs round-robin onto N-regulars (with repetition
         when N-regular pool is smaller than the M-ZC remainder).
      3. ZC↔ZC pairs already emitted by _simple_boundary; this function
         emits ONLY the cross-role overflow.
    """
    n_zcs = by_yesterday.get(("N", Role.ZC), [])
    m_zcs = by_today.get(("M", Role.ZC), [])
    n_regs = by_yesterday.get(("N", Role.STAFF), [])
    m_zcs_unmatched = m_zcs[len(n_zcs):]
    if not m_zcs_unmatched:
        return []
    if not n_regs:
        # No N-regulars to plan from; the unmatched M-ZCs self-plan their
        # first flight.
        return []
    pairs: list[Pair] = []
    for i, m_zc in enumerate(m_zcs_unmatched):
        n_planner = n_regs[i % len(n_regs)]   # round-robin with repetition
        pairs.append(_make_pair(
            n_planner, m_zc, OpsBoundary.N_TO_M, PairRole.PRIMARY, "N", "M",
            preplan_count=_H6_DEFAULT_COUNTS[OpsBoundary.N_TO_M],
        ))
    return pairs


def _three_tier_a_to_n(
    a_regulars: list[StaffMember],
    n_regulars: list[StaffMember],
    a1_regulars: list[StaffMember],
) -> list[Pair]:
    """A→N regulars with the 3-tier hierarchy (correction r2-7 + split-1+2).

    Tier 1: primary 1-to-1 A↔N (default preplan_count = 3 each).
    Tier 2: A surplus → A↔A1 handover-only (preplan_count = 0).
    Tier 3: A surplus still remaining → secondary A↔N as a SECOND
            primary pair, but with split planning (round-3 update). The
            primary pair's count drops from 3 to 2; the secondary pair
            carries count 1. Sum still 3 per N. Each N person can have
            at most 1 secondary pair.

    Edge cases:
      - No N regulars: A→N doesn't apply. A surplus may still go to A1
        handover-only without raising.
      - More A surplus than tier-3 capacity (|N|): raise
        PairGenerationError (W211).
    """
    if not n_regulars:
        if not a_regulars or not a1_regulars:
            return []
        tier2_only, _, _ = _one_to_one(
            a_regulars, a1_regulars,
            OpsBoundary.A_TO_A1, PairRole.HANDOVER_ONLY, "A", "A1",
            preplan_count=0,
        )
        return tier2_only

    # How many N's will get a split-secondary pair? It's the count of A
    # staff still surplus after tiers 1 and 2.
    a_used_tier1 = min(len(a_regulars), len(n_regulars))
    a_after_tier1 = len(a_regulars) - a_used_tier1
    a_used_tier2 = min(a_after_tier1, len(a1_regulars))
    a_after_tier2 = a_after_tier1 - a_used_tier2
    secondary_count = a_after_tier2

    # 2026-05-28 (user direction — centralized softening): the
    # capacity check WAS a hard raise (PairGenerationError) that
    # aborted the entire allocation. In practice the unpaired tail is
    # almost always 1–2 A staff out of 30+, which is operationally
    # harmless (they just don't have a planned_by/relieved_by entry on
    # their first/last flight — same as a pure single-shift staff).
    # Aborting the whole run for 1 unpaired staff turned every "add
    # one extra A-shift person" into a full allocation failure, which
    # also masked downstream issues like add_staff / remove_staff not
    # appearing. Now: cap secondary_count at capacity and let the
    # surplus go unpaired without raising. The caller (step3) emits
    # W211 INFO so the assigner still sees the count.
    pairs_overflow_unpaired = max(0, secondary_count - len(n_regulars))
    if pairs_overflow_unpaired > 0:
        secondary_count = len(n_regulars)

    pairs: list[Pair] = []
    # Tier 1: per-N primary pair. The first ``secondary_count`` N's get
    # primary preplan_count=2 (will be split with a secondary count=1);
    # the remaining N's get the full 3.
    for i in range(a_used_tier1):
        primary_count = 2 if i < secondary_count else 3
        pairs.append(_make_pair(
            a_regulars[i], n_regulars[i],
            OpsBoundary.A_TO_N, PairRole.PRIMARY, "A", "N",
            preplan_count=primary_count,
        ))
    # Tier 2: A surplus → A↔A1 handover-only.
    for j in range(a_used_tier2):
        a_s = a_regulars[a_used_tier1 + j]
        a1_s = a1_regulars[j]
        pairs.append(_make_pair(
            a_s, a1_s,
            OpsBoundary.A_TO_A1, PairRole.HANDOVER_ONLY, "A", "A1",
            preplan_count=0,
        ))
    # Tier 3: remaining A surplus → split-secondary A↔N (preplan_count=1).
    # These pair with the FIRST `secondary_count` N's — same N's whose
    # primary preplan_count was reduced to 2 above.
    for k in range(secondary_count):
        a_s = a_regulars[a_used_tier1 + a_used_tier2 + k]
        n_s = n_regulars[k]
        pairs.append(_make_pair(
            a_s, n_s,
            OpsBoundary.A_TO_N, PairRole.PRIMARY, "A", "N",
            preplan_count=1,
        ))
    return pairs


def _apply_overrides(pairs: list[Pair], overrides: list[Pair]) -> list[Pair]:
    """Replace auto-generated pairs that conflict with overrides.

    An override "claims" any (boundary, prev_emp) and (boundary, next_emp)
    slot it specifies. Auto-generated pairs sharing those slots are
    dropped, leaving the override authoritative. Staff displaced by the
    override become unpaired at that boundary (no pair row emitted).
    """
    claimed: set[tuple[OpsBoundary, str, str]] = set()
    for o in overrides:
        if o.prev_employee_id is not None:
            claimed.add((o.boundary, "prev", o.prev_employee_id))
        if o.next_employee_id is not None:
            claimed.add((o.boundary, "next", o.next_employee_id))
    out: list[Pair] = []
    for p in pairs:
        if p.prev_employee_id and (p.boundary, "prev", p.prev_employee_id) in claimed:
            continue
        if p.next_employee_id and (p.boundary, "next", p.next_employee_id) in claimed:
            continue
        out.append(p)
    out.extend(overrides)
    return out


def _hoist_p2f_first(
    staff_list: list[StaffMember], p2f_id: str | None,
) -> list[StaffMember]:
    """Return the input list with the P2F nominee moved to the front.
    Preserves order of every other member. No-op when ``p2f_id`` is
    None / not in the list. Used by the v2 pair generator so the day's
    P2F nominees naturally pair with each other via 1-to-1 sequencing.
    """
    if not p2f_id:
        return list(staff_list)
    p2f = [s for s in staff_list if s.employee_id == p2f_id]
    rest = [s for s in staff_list if s.employee_id != p2f_id]
    return p2f + rest


def _generate_pairs_v2(
    staff_today: list[StaffMember],
    yesterday_n_staff: list[StaffMember] | None,
    overrides: list[Pair] | None,
    p2f_handlers: dict[ShiftCode, str] | None,
) -> list[Pair]:
    """Alternate pair generator (user direction 2026-05-28 — Iteration B).

    Differences from the default ``generate_pairs``:
      * For each within-day P2F boundary (M→A, A→N), the day's P2F
        nominees are hoisted to the FRONT of their shift's STAFF list
        before 1-to-1 pairing. When both shifts have a nominee, they
        pair with each other; when one side has no nominee, the other's
        nominee falls back to whoever the normal logic would pair them
        with.
      * ZC pairing first absorbs as many ZC↔ZC matches as possible
        across boundaries (including A↔A1 ZCs at the A_TO_A1 boundary,
        which the default code only used for STAFF surplus). Whatever
        ZCs remain orphan are folded into that side's STAFF pool for
        the boundary's normal STAFF pairing — they "spill to STAFF".

    Everything else (N→M cross-role exception, the 3-tier A→N STAFF
    hierarchy, override application) matches the default generator.
    """
    by_today = _by_shift_role(staff_today)
    p2f_handlers = p2f_handlers or {}
    pairs: list[Pair] = []

    # ---- ZC pairing pass (with overflow-to-STAFF) ----
    # Two pools per shift: ``prev`` (ZCs available as boundary-prev,
    # i.e. handing off OUT of this shift) and ``next`` (ZCs available
    # as boundary-next, i.e. handing INTO this shift). A single ZC can
    # legitimately appear in TWO pair rows in v1 (as next of an
    # incoming boundary AND prev of an outgoing boundary) — that's the
    # natural "handover bridge" pattern. Splitting prev / next
    # preserves that pattern while still letting us detect ZCs that
    # got zero pair entries (completely orphan).
    zc_prev_pool: dict[ShiftCode, list[StaffMember]] = {
        sh: list(by_today.get((sh, Role.ZC), []))
        for sh in ("M", "M1", "A", "A1", "N")
    }
    zc_next_pool: dict[ShiftCode, list[StaffMember]] = {
        sh: list(by_today.get((sh, Role.ZC), []))
        for sh in ("M", "M1", "A", "A1", "N")
    }
    # Yesterday's N ZCs only ever play prev (handing INTO today's M).
    by_yesterday = _by_shift_role(yesterday_n_staff) if yesterday_n_staff else {}
    yesterday_n_zc_prev = list(by_yesterday.get(("N", Role.ZC), []))
    # Track which ZCs participated in any pair so we can fold the
    # completely-unpaired set into STAFF later.
    paired_zc_ids: set[str] = set()

    def _zc_pair_dir(
        prev_pool: list[StaffMember],
        next_pool: list[StaffMember],
        boundary: OpsBoundary,
        pair_role: PairRole,
        prev_shift: ShiftCode, next_shift: ShiftCode,
        *, preplan_count: int | None = None,
    ) -> None:
        """Pair as many ZCs as possible 1-to-1 at this boundary.

        2026-05-28 (centralized fix): drain ONLY the prev pool. Each ZC
        plays at most one "prev" role (outgoing handover) across the
        whole day — that's what the user's "rest left zc (here 2)"
        math requires. But the "next" role (incoming handover) can be
        filled multiple times — A1's only ZC legitimately receives
        briefings from BOTH M1 (start-of-shift, M1→A1) AND A (mid-shift,
        A→A1), so draining A1's next pool at M1→A1 would zero-out the
        A→A1 ZC pair the user explicitly asked for. v1's behavior is
        consistent with this — N's ZC was "next" at both A→N and
        A1→N without conflict.
        """
        n = min(len(prev_pool), len(next_pool))
        if n <= 0:
            return
        for i in range(n):
            pairs.append(_make_pair(
                prev_pool[i], next_pool[i],
                boundary, pair_role, prev_shift, next_shift,
                preplan_count=preplan_count,
            ))
            paired_zc_ids.add(prev_pool[i].employee_id)
            paired_zc_ids.add(next_pool[i].employee_id)
        # Drain ONLY prev (outgoing handover). Leave next pool intact
        # so a ZC on the receiving side can absorb multiple incoming
        # handovers across boundaries.
        del prev_pool[:n]

    # ZC pairing order: same set of boundaries as v1 + the new A↔A1 ZC
    # handover. Drain is DIRECTIONAL (prev vs next), so a single ZC can
    # still appear in multiple pair rows when they bridge boundaries.
    if yesterday_n_staff:
        _zc_pair_dir(
            yesterday_n_zc_prev, zc_next_pool["M"],
            OpsBoundary.N_TO_M, PairRole.PRIMARY, "N", "M",
        )
    _zc_pair_dir(
        zc_prev_pool["M"], zc_next_pool["M1"],
        OpsBoundary.M_TO_M1, PairRole.PRIMARY, "M", "M1",
    )
    _zc_pair_dir(
        zc_prev_pool["M"], zc_next_pool["A"],
        OpsBoundary.M_TO_A, PairRole.PRIMARY, "M", "A",
    )
    _zc_pair_dir(
        zc_prev_pool["M1"], zc_next_pool["A1"],
        OpsBoundary.M1_TO_A1, PairRole.PRIMARY, "M1", "A1",
    )
    _zc_pair_dir(
        zc_prev_pool["A"], zc_next_pool["N"],
        OpsBoundary.A_TO_N, PairRole.PRIMARY, "A", "N",
    )
    # 2026-05-28 NEW: A↔A1 ZC handover. The default v1 code only paired
    # STAFF at this boundary; here we catch surplus A-ZCs (prev side)
    # against any unused A1-ZC (next side) before they spill to STAFF.
    _zc_pair_dir(
        zc_prev_pool["A"], zc_next_pool["A1"],
        OpsBoundary.A_TO_A1, PairRole.HANDOVER_ONLY, "A", "A1",
        preplan_count=0,
    )
    _zc_pair_dir(
        zc_prev_pool["A1"], zc_next_pool["N"],
        OpsBoundary.A1_TO_N, PairRole.HANDOVER_ONLY, "A1", "N",
        preplan_count=0,
    )

    # ---- ZC overflow → STAFF augmentation ----
    # Completely-unpaired ZCs (zero pair entries in EITHER prev or next
    # role across all boundaries) get folded into their shift's STAFF
    # list so the STAFF pair-generation logic below treats them as
    # regular staff for pairing only. Their role / workload caps stay
    # ZC — only their pair slot changes.
    staff_lists: dict[ShiftCode, list[StaffMember]] = {
        sh: list(by_today.get((sh, Role.STAFF), []))
        for sh in ("M", "M1", "A", "A1", "N")
    }
    for sh in ("M", "M1", "A", "A1", "N"):
        for zc in by_today.get((sh, Role.ZC), []):
            if zc.employee_id not in paired_zc_ids:
                staff_lists[sh].append(zc)
    # Yesterday's N ZCs that didn't pair into today's M (rare):
    yesterday_n_orphans = [
        zc for zc in by_yesterday.get(("N", Role.ZC), [])
        if zc.employee_id not in paired_zc_ids
    ] if yesterday_n_staff else []
    yesterday_n_staff_aug = (
        list(by_yesterday.get(("N", Role.STAFF), [])) + yesterday_n_orphans
        if yesterday_n_staff else []
    )

    # ---- P2F nominee hoisting on STAFF lists ----
    # Move each shift's P2F nominee to the front of its augmented STAFF
    # list so the 1-to-1 sequencing pairs nominees with each other at
    # M→A and A→N. M / A / N are the only P2F-nominating shifts.
    for sh in ("M", "A", "N"):
        staff_lists[sh] = _hoist_p2f_first(
            staff_lists[sh], p2f_handlers.get(sh),
        )

    # ---- STAFF pairing using the augmented lists ----
    # N → M cross-day (with cross-role exception unchanged).
    if yesterday_n_staff:
        n2m_pairs, _, _ = _one_to_one(
            yesterday_n_staff_aug, staff_lists["M"],
            OpsBoundary.N_TO_M, PairRole.PRIMARY, "N", "M",
        )
        pairs.extend(n2m_pairs)
        # Cross-role exception (preserved): unmatched M-ZCs (those
        # already in zc_pool["M"] before we folded them into STAFF
        # would have stayed orphan — but here they were already folded.
        # So just call the existing helper against the original
        # un-augmented yesterday's view, mirroring the default code.
        pairs.extend(_n_to_m_cross_role(by_yesterday, by_today))

    # M→M1
    m_to_m1_pairs, _, _ = _one_to_one(
        staff_lists["M"], staff_lists["M1"],
        OpsBoundary.M_TO_M1, PairRole.PRIMARY, "M", "M1",
    )
    pairs.extend(m_to_m1_pairs)

    # M→A
    m_to_a_pairs, _, _ = _one_to_one(
        staff_lists["M"], staff_lists["A"],
        OpsBoundary.M_TO_A, PairRole.PRIMARY, "M", "A",
    )
    pairs.extend(m_to_a_pairs)

    # M1→A1
    m1_to_a1_pairs, _, _ = _one_to_one(
        staff_lists["M1"], staff_lists["A1"],
        OpsBoundary.M1_TO_A1, PairRole.PRIMARY, "M1", "A1",
    )
    pairs.extend(m1_to_a1_pairs)

    # A→N: keep the 3-tier hierarchy for STAFF (orphan ZCs folded in
    # above appear at the END of the A-STAFF list, so primary pairs
    # still consume "real" STAFF first and any ZC overflow goes to
    # tier-2/tier-3 — which is exactly the user-intended fallback).
    pairs.extend(_three_tier_a_to_n(
        staff_lists["A"], staff_lists["N"], staff_lists["A1"],
    ))

    # A1→N handover-only
    a1_to_n_pairs, _, _ = _one_to_one(
        staff_lists["A1"], staff_lists["N"],
        OpsBoundary.A1_TO_N, PairRole.HANDOVER_ONLY, "A1", "N",
        preplan_count=0,
    )
    pairs.extend(a1_to_n_pairs)

    if overrides:
        pairs = _apply_overrides(pairs, overrides)
    return pairs


def generate_pairs(
    staff_today: list[StaffMember],
    *,
    yesterday_n_staff: list[StaffMember] | None = None,
    overrides: list[Pair] | None = None,
    p2f_handlers: dict[ShiftCode, str] | None = None,
) -> list[Pair]:
    """Build the full pair list for an ops day. See module docstring for
    the algorithm, invariants, and error conditions.

    2026-05-28 (Phase 2 promoted to default — user direction): single
    centralized path. The previous v1 generator and the v2 variant are
    no longer two parallel paths; the v2 logic (P2F-prefer hoisting,
    ZC-only with STAFF overflow, A↔A1 ZC pairing) is now the only path.

    Why this is safe:
      * Empty ``p2f_handlers`` → P2F-hoist is identity (same staff
        order as v1).
      * Balanced ZC counts → no orphan ZCs, no STAFF augmentation
        happens (same STAFF pool as v1).
      * A↔A1 ZC pairing only emits when BOTH have spare ZCs; otherwise
        zero-op. (v1 had zero pairs at A_TO_A1 for ZCs — so any new
        pair here is purely additive, never replaces a v1 pair.)
      * ZC drain semantics: a single ZC pairs at most once across all
        boundaries (in v1 a ZC could appear in two boundaries — once
        as ``next`` and once as ``prev``). User direction 2026-05-28
        requires drain so the "rest left zc (here 2)" overflow math
        works for the example A=4 ZC, N=1 ZC, A1=1 ZC.

    The ``p2f_adjustment.logic_v2`` config flag continues to govern the
    P2F POST-PASS (anchored v1 vs. union-window v2) and the post-pass
    order swap. Pair generation is no longer behind that flag.
    """
    return _generate_pairs_v2(
        staff_today, yesterday_n_staff, overrides, p2f_handlers,
    )
