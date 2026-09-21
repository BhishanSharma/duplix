"""INTL post-pass — Phase 3 (2026-05-14, INTL overhaul).

The post-pass runs AFTER the CP-SAT solver returns and AFTER
``assemble_allocation_rows`` builds the initial AllocationRow list.

For each INTL DEP flight (``is_international=True`` under the Phase 3
DEP-only redefinition — Change 9), the post-pass:

  * Removes **1 normal-domestic preceding flight** from the same
    handler's chronological list, sending it to the §4 redistribution
    pool. (Change 2/3 narrowed mechanics — was "any INTL", is now
    "INTL DEP".)
  * **Escalates to 2 preceding flights** if the same handler already
    has a prior INTL DEP within 15-20 min (Change 3 — extra buffer for
    tight INTL-INTL clusters).
  * P2F and NORSE preceding flights are **skipped over** — only
    normal-domestic flights are eligible for removal.

§2.A (the 5-case cross-shift preplan table) was DELETED in Phase 3
because Change 4 (Phase 2-landed) made cross-shift INTL planning
impossible (INTL flights now self-pair: PLANNED_BY = RELIEVED_BY =
staff). The case-table matched nothing, so it's dead code.

The hard `<15 min` same-handler INTL DEP→INTL DEP constraint lives in
the solver (Change 5, Phase 4 pending). This post-pass takes whatever
the solver produced and applies the buffer rule.

Design notes lived in ``docs/intl_overhaul_2026-05-14.md`` (not included in
this repository snapshot).
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_t

from ..schemas import (
    AllocationRow,
    FlightInput,
    OpsClass,
    Role,
    ShiftCode,
    StaffMember,
)
from .caps import hard_cap_for
from .eligibility import sheet_target_for
from .windows import (
    SPACING_HARD_MIN,
    required_spacing_min,
    std_to_ops_day_minutes,
)

# Aviation pre-plan offset: planning happens at D-75 (75 min before STD).
# Kept here for re-export — eligibility.py owns the canonical constant
# and the coverage helper; postpass_intl re-imports them where needed.
PREPLAN_OFFSET_MIN = 75

# Phase 3 / Change 3 (2026-05-14): preceding-flight removal escalates
# from 1 to 2 when the same handler's prior INTL DEP is within this
# gap band (inclusive endpoints, in minutes).
INTL_TIGHT_GAP_MIN_MIN = 15
INTL_TIGHT_GAP_MIN_MAX = 20


def compute_removal_count(prev_intl_gap_min: int | None) -> int:
    """Phase 3 / Change 3 — preceding-flight removal count.

    Returns 2 when the same handler's previous INTL DEP is in the
    "tight" band ``[15, 20]`` minutes earlier (extra buffer needed);
    returns 1 otherwise (default — including the case where the
    current INTL DEP is the handler's first of the day).

    Note: same-handler INTL DEP pairs with gap < 15 min are rejected
    by the solver as a hard constraint (Change 5, Phase 4). The post-
    pass treats them as the default 1-removal case if any slip through.
    """
    if prev_intl_gap_min is None:
        return 1
    if INTL_TIGHT_GAP_MIN_MIN <= prev_intl_gap_min <= INTL_TIGHT_GAP_MIN_MAX:
        return 2
    return 1


def _eligible_for_removal(flight: FlightInput | None) -> bool:
    """Phase 3 / Change 2 — a flight is eligible to be displaced by the
    INTL DEP preceding-flight rule iff it's a plain-domestic flight
    (not INTL, not P2F, not NORSE).

    Renamed from ``_is_plain_domestic`` for clarity per the Change 2
    amendment that scopes the removable-prior pool to normal domestic
    flights only (skips P2F + NORSE).
    """
    if flight is None:
        return False
    if flight.is_international:
        return False
    return flight.ops_class not in (OpsClass.P2F, OpsClass.NORSE)


@dataclass
class PostPassSummary:
    """Diagnostic counts + workload reason notes for the audit harness.

    Phase 3 cleanup (2026-05-14): §2.A counters (``n_case_reassign``,
    ``n_case_migrate``, ``n_fallback_to_2b``) were dropped — the
    case-table is gone, the counters can't ever fire. ``n_case_2b``
    is kept as the canonical preceding-flight-removal counter.
    """

    n_intl_processed: int = 0
    n_case_2b: int = 0          # preceding-flight removals applied
    n_redistributed: int = 0    # pool flights successfully re-homed
    n_redistribute_failed: int = 0
    workload_notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    log_lines: list[str] = field(default_factory=list)

    def note(self, staff_id: str, reason: str) -> None:
        self.workload_notes[staff_id].append(reason)

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)


# ---------- §4 most-eligible helper (still used by postpass_p2f) ----------

SPACING_MIN = 15  # H10 hard min, mirrored from windows.SPACING_HARD_MIN


def _most_eligible(
    flight: FlightInput,
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    current_counts: dict[str, int],
    excluded_recipients: set[str],
    preferred_shift: ShiftCode | None,
    p2f_handlers: dict[ShiftCode, str],
    norse_handlers: set[str],
    staff_stds: dict[str, list[int]],
    flight_std_min: int,
    target_max: int = 22,
    hard_cap: int = 24,
) -> StaffMember | None:
    """Pick the most-eligible staff for ``flight`` (user §4 + H10 guard).

    Filters in priority order:
      1. Shift's STD window covers this flight (matrix encodes this)
      2. ``current_counts[s] < target_max`` (has room below target)
      3. P2F → P2F-licensed only; NORSE → nominated handler only
      4. Not in ``excluded_recipients`` (no cascading post-pass)
      5. Hard cap holds
      6. Eligibility matrix includes them for this flight
      7. **H10 spacing**: no existing flight within ±15 min of this STD

    Tie-break: lowest current count, then alphabetical by name.

    When ``preferred_shift`` is set, restrict to that shift first; if
    no one in that shift qualifies, expand to all shifts whose window
    covers the STD.
    """
    eligible_set = matrix.get(flight.unique_id, set())
    norse_required = flight.ops_class == OpsClass.NORSE
    p2f_required = flight.ops_class == OpsClass.P2F

    def _candidate(s: StaffMember) -> bool:
        if s.employee_id in excluded_recipients:
            return False
        if s.shift_today is None or s.role == Role.AM:
            return False
        if s.employee_id not in eligible_set:
            return False
        staff_cap = hard_cap_for(s)
        if current_counts.get(s.employee_id, 0) >= staff_cap:
            return False
        if p2f_required and not s.is_p2f_licensed:
            return False
        if norse_required and norse_handlers and s.employee_id not in norse_handlers:
            return False
        existing = staff_stds.get(s.employee_id, ())
        # Patch 2026-05-15: band-aware H10 floor. ``required_spacing_min``
        # returns 10 when both flights' STDs are in the relaxed bands,
        # else 15. Matches the solver's per-flight IntervalVar length.
        return all(
            abs(std - flight_std_min) >= required_spacing_min(std, flight_std_min)
            for std in existing
        )

    def _pick_from(pool: list[StaffMember]) -> StaffMember | None:
        under = [s for s in pool if current_counts.get(s.employee_id, 0) < target_max]
        if under:
            under.sort(key=lambda s: (current_counts.get(s.employee_id, 0), s.name))
            return under[0]
        if pool:
            pool.sort(key=lambda s: (current_counts.get(s.employee_id, 0), s.name))
            return pool[0]
        return None

    candidates = [s for s in staff_today if _candidate(s)]
    if preferred_shift is not None:
        preferred_pool = [s for s in candidates if s.shift_today == preferred_shift]
        pick = _pick_from(preferred_pool)
        if pick is not None:
            return pick
    return _pick_from(candidates)


# ---------- main entry point ----------

def apply_intl_post_pass(
    rows: list[AllocationRow],
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    elig_ctx_p2f_handlers: dict[ShiftCode, str],
    elig_ctx_norse_handlers: set[str],
    d_day: date_t,
    skip_intl_removal_keys: frozenset[str] = frozenset(),
) -> tuple[list[AllocationRow], PostPassSummary]:
    """Apply Phase 3 INTL post-pass (§2.B with band escalation) to
    ``rows``. Returns updated rows and a summary the orchestrator can
    fold into OUT_Workload_Summary.

    For each INTL DEP flight (sorted by STD ascending):
      - Compute the same handler's previous INTL DEP gap (if any).
      - ``compute_removal_count(gap)`` decides 1 or 2 removals.
      - Remove that many of the host's chronologically PREVIOUS
        normal-domestic flights (skip P2F + NORSE); each removed
        flight goes to §4 redistribution.
    """
    summary = PostPassSummary()
    staff_by_id = {s.employee_id: s for s in staff_today}
    flight_by_uid = {f.unique_id: f for f in flights}

    # Mutable working state. We key by the AllocationRow's reconstructed
    # uid (matches FlightInput.unique_id) so we can swap fields without
    # rebuilding immutable AllocationRow each iteration.
    rstate: dict[str, dict] = {}
    rows_input_count = len(rows)
    rows_with_no_flight_lookup: list[str] = []
    for r in rows:
        uid = (
            f"{r.flt}|{r.dep}|{r.arr}|{r.std.isoformat(timespec='minutes')}"
            f"|{r.date.isoformat()}"
        )
        if uid not in flight_by_uid:
            rows_with_no_flight_lookup.append(uid)
        rstate[uid] = {
            "row": r,
            "staff_id": r.staff_employee_id,
            "staff_name": r.staff_name,
            "planned_by_emp": r.planned_by_employee_id,
            "planned_by_name": r.planned_by_name,
            "relieved_by_emp": r.relieved_by_employee_id,
            "relieved_by_name": r.relieved_by_name,
            "warning": r.warning,
            "sheet_target": r.sheet_target,
            "uid": uid,
        }
    if len(rstate) != rows_input_count:
        summary.log(
            f"WARNING: rstate has {len(rstate)} entries from "
            f"{rows_input_count} input rows — duplicate uids detected"
        )
    if rows_with_no_flight_lookup:
        summary.log(
            f"WARNING: {len(rows_with_no_flight_lookup)} rows have no "
            f"matching FlightInput (will rebuild from row alone). "
            f"sample uids: {rows_with_no_flight_lookup[:3]}"
        )

    # Current per-staff count (uid set) — used by §4 cap check.
    counts: dict[str, set[str]] = defaultdict(set)
    for uid, st in rstate.items():
        if st["staff_id"]:
            counts[st["staff_id"]].add(uid)
    current_counts = {sid: len(uids) for sid, uids in counts.items()}

    # Per-staff sorted STD list (in ops-day minutes) for H10 enforcement
    # in §4 most-eligible. Refreshed every time we move a flight between
    # staff so each new redistribution sees the up-to-date schedule.
    staff_stds: dict[str, list[int]] = defaultdict(list)
    for uid, st in rstate.items():
        if not st["staff_id"]:
            continue
        f_ref = flight_by_uid.get(uid)
        if f_ref is None:
            continue
        staff_stds[st["staff_id"]].append(
            std_to_ops_day_minutes(f_ref.std, f_ref.date, d_day),
        )

    # Recipients-already-touched: §4 forbids cascading.
    excluded: set[str] = set()

    # Iterate INTL flights deterministically (STD asc).
    intl_uids = sorted(
        (uid for uid, st in rstate.items()
         if flight_by_uid.get(uid) and flight_by_uid[uid].is_international
         and st["staff_id"]),
        key=lambda u: std_to_ops_day_minutes(
            flight_by_uid[u].std, flight_by_uid[u].date, d_day,
        ),
    )
    summary.n_intl_processed = len(intl_uids)

    for uid in intl_uids:
        f = flight_by_uid[uid]
        st = rstate[uid]
        host = staff_by_id.get(st["staff_id"])
        if host is None or host.shift_today is None:
            continue

        # Phase R override: skip preceding-flight removal entirely for
        # this INTL DEP if the operator approved it via an override.
        flight_key = f"{f.flt}|{f.std.isoformat(timespec='minutes')}"
        if flight_key in skip_intl_removal_keys:
            summary.log(
                f"INTL flt {f.flt}: preceding-removal SKIPPED by "
                f"an override (skip_intl_removal)"
            )
            continue

        # Compute the same-handler previous INTL gap (Change 3).
        intl_std_min = std_to_ops_day_minutes(f.std, f.date, d_day)
        prev_intl_std_min: int | None = None
        for u2, sx2 in rstate.items():
            if u2 == uid or sx2["staff_id"] != host.employee_id:
                continue
            f2 = flight_by_uid.get(u2)
            if f2 is None or not f2.is_international:
                continue
            f2_min = std_to_ops_day_minutes(f2.std, f2.date, d_day)
            if f2_min < intl_std_min and (
                prev_intl_std_min is None or f2_min > prev_intl_std_min
            ):
                prev_intl_std_min = f2_min
        prev_gap = (
            intl_std_min - prev_intl_std_min
            if prev_intl_std_min is not None else None
        )
        n_to_remove = compute_removal_count(prev_gap)

        _apply_preceding_removal(
            f, uid, host, n_to_remove, rstate, current_counts,
            excluded, summary, flight_by_uid, staff_today, matrix, d_day,
            elig_ctx_p2f_handlers, elig_ctx_norse_handlers, staff_stds,
        )

    # Rebuild the rows list from rstate.
    new_rows: list[AllocationRow] = []
    n_kept_original = 0
    for uid, st in rstate.items():
        f = flight_by_uid.get(uid)
        if f is None:
            new_rows.append(st["row"])
            n_kept_original += 1
            continue
        new_rows.append(AllocationRow(
            date=f.date, flt=f.flt, dep=f.dep, arr=f.arr, std=f.std,
            pax=f.load,
            staff_employee_id=st["staff_id"] or "",
            staff_name=st["staff_name"] or "",
            planned_by_employee_id=st["planned_by_emp"],
            planned_by_name=st["planned_by_name"],
            relieved_by_employee_id=st["relieved_by_emp"],
            relieved_by_name=st["relieved_by_name"],
            warning=st["warning"],
            sheet_target=st["sheet_target"],
            is_international=f.is_international,
        ))
    if len(new_rows) != rows_input_count:
        summary.log(
            f"!! ROW COUNT MISMATCH: input={rows_input_count} "
            f"output={len(new_rows)} kept_original={n_kept_original}"
        )
    return new_rows, summary


# ---------- preceding-flight removal (Phase 3 §2.B with bands) ----------

def _set_assignee(
    st: dict, new_staff: StaffMember,
    counts: dict[str, int], old_staff_id: str | None,
    staff_stds: dict[str, list[int]], flight_std_min: int,
) -> None:
    """Move the row's staff field from ``old_staff_id`` to ``new_staff``.

    Also maintains ``staff_stds`` (per-staff STD list) so subsequent
    §4 most-eligible calls correctly enforce H10 spacing.
    """
    if old_staff_id:
        counts[old_staff_id] = max(0, counts.get(old_staff_id, 0) - 1)
        if old_staff_id in staff_stds:
            with contextlib.suppress(ValueError):
                staff_stds[old_staff_id].remove(flight_std_min)
    counts[new_staff.employee_id] = counts.get(new_staff.employee_id, 0) + 1
    staff_stds.setdefault(new_staff.employee_id, []).append(flight_std_min)
    st["staff_id"] = new_staff.employee_id
    st["staff_name"] = new_staff.name


def _apply_preceding_removal(
    f: FlightInput,
    uid: str,
    host: StaffMember,
    n_to_remove: int,
    rstate: dict[str, dict],
    counts: dict[str, int],
    excluded: set[str],
    summary: PostPassSummary,
    flight_by_uid: dict[str, FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    d_day: date_t,
    p2f_handlers: dict[ShiftCode, str],
    norse_handlers: set[str],
    staff_stds: dict[str, list[int]],
) -> None:
    """Phase 3 / Changes 2 + 3 — remove up to ``n_to_remove`` of the
    host's chronologically previous **normal-domestic** flights and
    redistribute each via §4. P2F and NORSE flights are skipped.
    """
    intl_std_min = std_to_ops_day_minutes(f.std, f.date, d_day)
    # Candidate set: host's flights with STD < intl_std_min, eligible
    # for removal (plain-domestic).
    prev_candidates: list[tuple[int, str]] = []
    for u, sx in rstate.items():
        if sx["staff_id"] != host.employee_id or u == uid:
            continue
        cand_f = flight_by_uid.get(u)
        if not _eligible_for_removal(cand_f):
            continue
        cand_min = std_to_ops_day_minutes(cand_f.std, cand_f.date, d_day)  # type: ignore[union-attr]
        if cand_min < intl_std_min:
            prev_candidates.append((cand_min, u))

    if not prev_candidates:
        summary.log(
            f"INTL flt {f.flt} (host={host.name}): no chronologically "
            f"previous normal-domestic flight to displace; skipping"
        )
        return

    # Pick closest preceding flights (largest std_min < intl_std_min) first.
    prev_candidates.sort(reverse=True)
    targets = prev_candidates[:n_to_remove]

    for displaced_std_min, displaced_uid in targets:
        displaced_f = flight_by_uid[displaced_uid]
        dst = rstate[displaced_uid]
        # Remove from host; pool via §4 (exclude host so flight doesn't
        # bounce back to them — guards against self-cycles).
        counts[host.employee_id] = max(0, counts.get(host.employee_id, 0) - 1)
        if host.employee_id in staff_stds:
            with contextlib.suppress(ValueError):
                staff_stds[host.employee_id].remove(displaced_std_min)
        excluded_for_pool = excluded | {host.employee_id}
        new_holder = _most_eligible(
            displaced_f, staff_today, matrix, counts, excluded_for_pool,
            None,
            p2f_handlers, norse_handlers, staff_stds, displaced_std_min,
        )
        if new_holder is None:
            # Restore the flight on the host (no eligible recipient found).
            counts[host.employee_id] += 1
            staff_stds.setdefault(host.employee_id, []).append(displaced_std_min)
            summary.n_redistribute_failed += 1
            summary.log(
                f"INTL flt {f.flt}: preceding-removal failed — no eligible "
                f"recipient for flt {displaced_f.flt}, keeping it with {host.name}"
            )
            # 2026-05-26 user direction: surface the failure on the
            # INTL row's warning column so the assigner sees at-a-
            # glance which INTL flights didn't get the preceding leg
            # cleared. The string text is intentionally the same as
            # the log line tail so a Ctrl-F across both surfaces
            # turns up the same matches.
            _intl_state = rstate.get(uid)
            if _intl_state is not None:
                _fail_msg = "preceding-removal failed — no eligible recipient"
                _existing = _intl_state.get("warning") or ""
                _intl_state["warning"] = (
                    f"{_existing}; {_fail_msg}" if _existing else _fail_msg
                )
            continue
        _set_assignee(
            dst, new_holder, counts, host.employee_id,
            staff_stds, displaced_std_min,
        )
        dst["sheet_target"] = sheet_target_for(displaced_f, new_holder)
        excluded.add(new_holder.employee_id)
        summary.n_redistributed += 1
        summary.n_case_2b += 1
        summary.note(
            host.employee_id,
            f"Below target: 1 flight removed for INTL FLT {f.flt}",
        )
        summary.log(
            f"INTL flt {f.flt}: removed flt {displaced_f.flt} from "
            f"{host.name} -> {new_holder.name} ({new_holder.shift_today})"
            f" (n_to_remove={n_to_remove})"
        )
