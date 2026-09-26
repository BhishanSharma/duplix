"""P2F workload adjustment post-pass (rewrite per user direction 2026-05-26).

Runs AFTER the INTL post-pass. For each P2F flight, displaces the
closest normal-domestic flight in the (anchor, anchor + 30 min] window
from the **host** (whichever P2F handler's shift owns the anchor),
not from the **owner** (who flies the P2F leg).

Anchors per P2F flight:
  * D-3hrs (P2F_STD - 180 min)
  * D-1hr  (P2F_STD - 60 min)
  * D+20m  (P2F_STD + 20 min)

Host selection (2026-05-26 redesign):
  * Anchor → shift via SHIFT_STD_WINDOW_INNER (M / A / N only — those
    are the shifts that nominate P2F handlers per the 2026-05-10
    directive). Role-agnostic: 12:55 → M, 13:00 → A.
  * Host = ``p2f_handlers[host_shift]``.
  * If host is a ZC and the anchor falls inside their ZC start- or
    end-of-shift report buffer (1.5 hr each side), SKIP that anchor —
    the ZC isn't taking flights there anyway, so there's nothing to
    displace and the briefing happens during their report time.
  * If no host shift covers the anchor (e.g. early-morning M-shift
    P2F at 06:00 → D-3hrs = 03:00, before any shift), skip with a
    log line — the W220 warning fires from step3 in parallel.

Displacement (2026-05-26 revised — no ±30 cap):
  * Closest displaceable flight strictly AFTER the anchor (any
    distance). The handler needs the time FROM the anchor onward
    to prep; flights before are already done.
  * If the host has NO displaceable flight after the anchor (e.g.
    P2F at 12:55 with D-1 at 11:55 and the host's day ends after a
    last 11:50 leg), fall back to the closest displaceable flight
    BEFORE the anchor. Removal is non-negotiable when any
    displaceable flight exists.

Warning surface:
  * Each P2F row's ``warning`` column lists the removed flights as
    ``FLT 7895@10:25, FLT 6118@12:10`` (just no + STD, per the
    2026-05-26 ask). Pair-based PLANNED_BY / RELIEVED_BY are blanked
    by relabel_pair_columns — operators read the warning column to
    see what prep cost this P2F incurred.
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
from .eligibility import sheet_target_for
from .postpass_intl import _most_eligible
from .windows import (
    SHIFT_STD_WINDOW_INNER,
    ZC_BUFFER_END_MIN,
    ZC_BUFFER_START_MIN,
    SpacingKey,
    spacing_key,
    std_to_ops_day_minutes,
)

# Anchor offsets relative to P2F_STD, paired with the number of
# flights to displace at each anchor. Each anchor displaces the K
# closest flights STRICTLY AFTER the anchor (no fallback to BEFORE,
# per the 2026-05-28 user direction).
#
# 2026-05-28 (user direction): counts changed from {1, 1, 1} to
# {2, 1, 1}, and the third anchor moved from D+20m to D (= P2F_STD).
# Removing 2 flights after D-3h gives the host the prep block they
# need; the +1 after D-1h covers final briefing; the +1 after D
# covers the immediate post-P2F slot (was D+20m before — now strictly
# the next flight after the P2F itself).
_ANCHORS: tuple[tuple[int, str, int], ...] = (
    (-3 * 60, "D-3h", 2),
    (-1 * 60, "D-1h", 1),
    (    0,   "D",    1),
)

# 2026-05-27 (centralization): pulled from schemas — single source
# of truth for which shifts nominate plain P2F handlers. M1 / A1 don't
# nominate per the 2026-05-10 directive — anchors in those windows
# fall through to whichever of M / A / N also covers them.
from ..schemas import P2F_HANDLER_ELIGIBLE_SHIFTS as _HOST_CANDIDATE_SHIFTS


def _anchor_host_shift(anchor_min: int) -> ShiftCode | None:
    """Map an anchor time (ops-day minutes) to a P2F-handler-bearing
    shift via SHIFT_STD_WINDOW_INNER. Returns None when the anchor
    falls in no candidate shift's window (W220 territory for M; for
    other shifts simply means no displacement happens at that anchor).

    Role-agnostic by design — 12:55 → M, 13:00 → A. Caller deals with
    ZC report-buffer skip separately.
    """
    for shift in _HOST_CANDIDATE_SHIFTS:
        lo, hi = SHIFT_STD_WINDOW_INNER[shift]
        if lo <= anchor_min <= hi:
            return shift
    return None


def _anchor_in_zc_report_buffer(
    host_shift: ShiftCode, anchor_min: int,
) -> bool:
    """True when ``anchor_min`` lands in either of ``host_shift``'s
    1.5 hr ZC report-buffer windows (start-of-shift or end-of-shift).

    Used to skip displacement when a ZC hosts an anchor — they have
    no flights to remove during their report time anyway, and the
    briefing slots in there naturally.
    """
    s1, e1 = ZC_BUFFER_START_MIN.get(host_shift, (0, 0))
    s2, e2 = ZC_BUFFER_END_MIN.get(host_shift, (0, 0))
    return (s1 <= anchor_min < e1) or (s2 <= anchor_min < e2)


@dataclass
class P2FPostPassSummary:
    """Diagnostic counters for the audit harness."""

    n_p2f_processed: int = 0
    n_removed: int = 0
    n_redistributed: int = 0
    n_redistribute_failed: int = 0
    n_anchor_shortage: int = 0  # window had fewer candidates than required
    workload_notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    log_lines: list[str] = field(default_factory=list)

    def note(self, staff_id: str, reason: str) -> None:
        self.workload_notes[staff_id].append(reason)

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)


def _is_displaceable(flight: FlightInput) -> bool:
    """A flight that the P2F post-pass is allowed to remove from the
    handler's allocation. Excludes:
      - P2F flights themselves (never displaced)
      - INTL DEP flights (Phase 3 / Change 4: same handler start-to-
        finish — INTL DEP allocations are sacred after the solver)

    Per spec §3: the post-pass removes flights from the handler's
    list AROUND the P2F flight — never the P2F flight itself.
    """
    if flight.ops_class == OpsClass.P2F:
        return False
    # Phase 3 (2026-05-14, INTL overhaul / Change 4): an INTL DEP
    # flight must stay with its assigned handler (self-pair). The P2F
    # post-pass cannot move it without breaking that invariant.
    return not flight.is_international


def apply_p2f_post_pass_v2(
    rows: list[AllocationRow],
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    p2f_handlers: dict[ShiftCode, str],
    d_day: date_t,
) -> tuple[list[AllocationRow], P2FPostPassSummary]:
    """Alternate P2F post-pass (user direction 2026-05-28).

    Simple union-window rule: for each P2F flight at STD=T, the chain
    ``{owner shift's P2F handler, pre-planner shift's P2F handler}``
    loses ALL flights with STD in ``[T-2h, T+1h]`` from their queues.
    The P2F flight itself stays with the owner. Removed flights enter
    §4 redistribution.

    Differences from the anchored v1:
      * No D-3h / D-1h / D+20m anchors. One contiguous window per P2F.
      * No ZC report-buffer skip — if a chain member is a ZC, their
        in-window flights still get removed (the buffer is a softer
        concept that the simplified model intentionally ignores).
      * Chain semantics handle the shift-boundary edge case the
        anchored model misses (e.g. P2F at 13:30, owner = A shift
        handler, has a 13:00 flight — that flight is in the chain and
        in [11:30, 14:30] so it gets removed even though 13:00 is in
        the pre-window).
      * planned_by on the P2F row = pre-planner (when different from
        owner). relieved_by stays blank — owner handles their own
        post-window since they're freed by the same window logic.

    Returns ``(new_rows, summary)`` — same shape as v1. Row count
    preserved (flights are reassigned, not dropped).
    """
    summary = P2FPostPassSummary()
    flight_by_uid = {f.unique_id: f for f in flights}

    rstate: dict[str, dict] = {}
    rows_input_count = len(rows)
    for r in rows:
        uid = (
            f"{r.flt}|{r.dep}|{r.arr}|{r.std.isoformat(timespec='minutes')}"
            f"|{r.date.isoformat()}"
        )
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

    counts: dict[str, int] = defaultdict(int)
    staff_stds: dict[str, list[SpacingKey]] = defaultdict(list)
    for uid, st in rstate.items():
        if not st["staff_id"]:
            continue
        f_ref = flight_by_uid.get(uid)
        if f_ref is None:
            continue
        counts[st["staff_id"]] += 1
        staff_stds[st["staff_id"]].append(spacing_key(f_ref, d_day))
    excluded: set[str] = set()

    p2f_uids = sorted(
        (uid for uid, st in rstate.items()
         if flight_by_uid.get(uid)
         and flight_by_uid[uid].ops_class == OpsClass.P2F
         and st["staff_id"]),
        key=lambda u: std_to_ops_day_minutes(
            flight_by_uid[u].std, flight_by_uid[u].date, d_day,
        ),
    )
    staff_by_id: dict[str, StaffMember] = {
        s.employee_id: s for s in staff_today if s.employee_id
    }

    # Resolve pre-planner shift from owner shift. Pre-shift mapping
    # mirrors the ops-day flow: N is preceded by A, A by M, M by N of
    # D-1 (which means no D-day chain partner — fall back to owner-only).
    _PRE_SHIFT: dict[ShiftCode, ShiftCode | None] = {
        "M": None,  # M's pre-window is D-1's N — no D-day chain partner
        "A": "M",
        "N": "A",
    }

    for p2f_uid in p2f_uids:
        p2f_f = flight_by_uid[p2f_uid]
        owner_id = rstate[p2f_uid]["staff_id"]
        if not owner_id:
            continue
        summary.n_p2f_processed += 1
        p2f_min = std_to_ops_day_minutes(p2f_f.std, p2f_f.date, d_day)
        window_lo = p2f_min - 120
        window_hi = p2f_min + 60

        owner_shift = _anchor_host_shift(p2f_min)
        if owner_shift is None:
            summary.n_anchor_shortage += 1
            summary.log(
                f"P2F flt {p2f_f.flt} v2: STD "
                f"{p2f_min // 60:02d}:{p2f_min % 60:02d} falls in no "
                "P2F-handler-bearing shift — chain empty, no removal"
            )
            continue
        pre_shift = _PRE_SHIFT.get(owner_shift)
        pre_planner_id = (
            p2f_handlers.get(pre_shift) if pre_shift else None
        )
        chain_ids = {owner_id}
        if pre_planner_id and pre_planner_id != owner_id:
            chain_ids.add(pre_planner_id)

        # Sort displaceable candidates chronologically so the
        # redistribution pool sees them in a stable order — matters
        # because _displace_one mutates counts / staff_stds as it goes.
        candidates: list[tuple[int, str]] = []  # (cand_min, uid)
        for u, sx in rstate.items():
            if u == p2f_uid:
                continue
            if sx["staff_id"] not in chain_ids:
                continue
            f_cand = flight_by_uid.get(u)
            if f_cand is None or not _is_displaceable(f_cand):
                continue
            cand_min = std_to_ops_day_minutes(
                f_cand.std, f_cand.date, d_day,
            )
            if window_lo <= cand_min <= window_hi:
                candidates.append((cand_min, u))
        candidates.sort()

        removed_for_this_p2f: list[tuple[str, str]] = []
        for _cand_min, cand_uid in candidates:
            holder_id = rstate[cand_uid]["staff_id"]
            if holder_id not in chain_ids:
                # Could have been moved by a prior P2F's pass in this loop
                continue
            displaced_f = flight_by_uid[cand_uid]
            displaced_std_iso = displaced_f.std.isoformat(timespec="minutes")
            _displace_one(
                cand_uid, p2f_f, holder_id, "v2-window", rstate,
                flight_by_uid, staff_today, matrix, counts,
                staff_stds, excluded, p2f_handlers,
                d_day, summary,
            )
            if rstate[cand_uid]["staff_id"] != holder_id:
                removed_for_this_p2f.append(
                    (str(displaced_f.flt), displaced_std_iso),
                )
                if holder_id == owner_id:
                    summary.note(
                        holder_id,
                        f"Below target: 1 flight removed for "
                        f"P2F FLT {p2f_f.flt} (v2 window)",
                    )
                else:
                    summary.note(
                        holder_id,
                        f"P2F prep host (v2): 1 flight removed for "
                        f"FLT {p2f_f.flt} (owner was "
                        f"{rstate[p2f_uid]['staff_name']})",
                    )

        # Warning column on P2F row: list removed flights.
        if removed_for_this_p2f:
            tag = ", ".join(
                f"FLT {flt}@{std}" for flt, std in removed_for_this_p2f
            )
            existing = rstate[p2f_uid].get("warning") or ""
            new_warn = (
                f"{existing}; Removed: {tag}"
                if existing else f"Removed: {tag}"
            )
            rstate[p2f_uid]["warning"] = new_warn

        # planned_by = pre-planner when different from owner.
        # relieved_by stays blank: owner is freed by the same window
        # so no separate post-pair host exists in v2.
        if pre_planner_id and pre_planner_id != owner_id:
            pre_staff = staff_by_id.get(pre_planner_id)
            if pre_staff is not None:
                rstate[p2f_uid]["planned_by_emp"] = pre_planner_id
                rstate[p2f_uid]["planned_by_name"] = pre_staff.name

    new_rows: list[AllocationRow] = []
    for uid, st in rstate.items():
        f = flight_by_uid.get(uid)
        if f is None:
            new_rows.append(st["row"])
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
            f"!! P2F v2 post-pass row count mismatch: "
            f"input={rows_input_count} output={len(new_rows)}"
        )
    return new_rows, summary


def apply_p2f_post_pass(
    rows: list[AllocationRow],
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    p2f_handlers: dict[ShiftCode, str],
    d_day: date_t,
    tolerance_minutes: int = 15,
) -> tuple[list[AllocationRow], P2FPostPassSummary]:
    """Apply §3 P2F workload adjustment to ``rows``.

    Returns ``(new_rows, summary)``. Row count is preserved — flights
    are reassigned (or kept on the handler if redistribution fails),
    never dropped.
    """
    summary = P2FPostPassSummary()
    flight_by_uid = {f.unique_id: f for f in flights}

    # Mutable working state — same shape as INTL post-pass.
    rstate: dict[str, dict] = {}
    rows_input_count = len(rows)
    for r in rows:
        uid = (
            f"{r.flt}|{r.dep}|{r.arr}|{r.std.isoformat(timespec='minutes')}"
            f"|{r.date.isoformat()}"
        )
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

    # Per-staff state for §4: counts + STD lists (for H10 spacing).
    counts: dict[str, int] = defaultdict(int)
    staff_stds: dict[str, list[SpacingKey]] = defaultdict(list)
    for uid, st in rstate.items():
        if not st["staff_id"]:
            continue
        f_ref = flight_by_uid.get(uid)
        if f_ref is None:
            continue
        counts[st["staff_id"]] += 1
        staff_stds[st["staff_id"]].append(spacing_key(f_ref, d_day))
    excluded: set[str] = set()

    # Iterate P2F flights chronologically.
    p2f_uids = sorted(
        (uid for uid, st in rstate.items()
         if flight_by_uid.get(uid)
         and flight_by_uid[uid].ops_class == OpsClass.P2F
         and st["staff_id"]),
        key=lambda u: std_to_ops_day_minutes(
            flight_by_uid[u].std, flight_by_uid[u].date, d_day,
        ),
    )

    # employee_id -> StaffMember for ZC-detection lookups
    staff_by_id: dict[str, StaffMember] = {
        s.employee_id: s for s in staff_today if s.employee_id
    }

    for p2f_uid in p2f_uids:
        p2f_f = flight_by_uid[p2f_uid]
        owner_id = rstate[p2f_uid]["staff_id"]
        if not owner_id:
            continue
        summary.n_p2f_processed += 1
        p2f_min = std_to_ops_day_minutes(p2f_f.std, p2f_f.date, d_day)
        # Track removed flights for this P2F's warning column.
        removed_for_this_p2f: list[tuple[str, str]] = []  # (flt, std_iso)
        # 2026-05-27 (user direction): record which host did the prep
        # before vs after the P2F STD so we can populate planned_by /
        # relieved_by on the P2F row. D-3 / D-1 → planned_by (host
        # does prep BEFORE the flight). D+20 → relieved_by (host does
        # paperwork AFTER). When the host is the owner themselves
        # (same person flying the leg), skip the annotation — a self-
        # pair label adds no information.
        pre_host_id: str | None = None       # D-3 preferred, D-1 fallback
        pre_host_name: str | None = None
        post_host_id: str | None = None      # D+20
        post_host_name: str | None = None

        for offset, label, anchor_count in _ANCHORS:
            anchor_min = p2f_min + offset
            # 2026-05-26 redesign: host = whichever shift's STD window
            # contains the anchor. Role-agnostic — staff or ZC, doesn't
            # matter for the mapping step.
            host_shift = _anchor_host_shift(anchor_min)
            if host_shift is None:
                summary.n_anchor_shortage += 1
                summary.log(
                    f"P2F flt {p2f_f.flt} ({label}): anchor "
                    f"{anchor_min // 60:02d}:{anchor_min % 60:02d} falls "
                    "in no P2F-handler-bearing shift window — no "
                    "displacement (W220 may also fire from step3)"
                )
                continue
            host_id = p2f_handlers.get(host_shift)
            if not host_id:
                summary.n_anchor_shortage += 1
                summary.log(
                    f"P2F flt {p2f_f.flt} ({label}): no P2F handler "
                    f"nominated for host shift {host_shift} — "
                    "anchor unhosted"
                )
                continue
            host_staff = staff_by_id.get(host_id)
            # 2026-05-27: record host for the P2F row's planned_by /
            # relieved_by columns. D-3 / D-1 → planned_by (work BEFORE
            # the flight), D → relieved_by (the host taking the
            # immediate post-P2F slot). Skip when host == owner (would
            # be a self-pair label). D-3 takes precedence over D-1; if
            # D-3 host is the owner, D-1's host fills the planned_by
            # slot. 2026-05-28: third anchor renamed D+20m → D.
            if host_id != owner_id:
                host_label = (
                    host_staff.name if host_staff is not None else host_id
                )
                if label == "D-3h":
                    pre_host_id = host_id
                    pre_host_name = host_label
                elif label == "D-1h" and pre_host_id is None:
                    pre_host_id = host_id
                    pre_host_name = host_label
                elif label == "D":
                    post_host_id = host_id
                    post_host_name = host_label
            # ZC report-buffer skip: if the host is a ZC and the anchor
            # falls inside their start- or end-of-shift 1.5 hr report
            # buffer, ZC isn't taking flights there anyway — skip
            # displacement (briefing slots into report time). Host
            # annotation is ALREADY recorded above, so the column will
            # still show who did the briefing.
            if (host_staff is not None
                    and host_staff.role == Role.ZC
                    and _anchor_in_zc_report_buffer(host_shift, anchor_min)):
                summary.log(
                    f"P2F flt {p2f_f.flt} ({label}): host "
                    f"{host_staff.name} ({host_shift}/ZC) — anchor "
                    f"{anchor_min // 60:02d}:{anchor_min % 60:02d} in "
                    "ZC report-buffer; briefing absorbed, no flight removed"
                )
                continue

            # 2026-05-28 (user direction): pick the K closest displaceable
            # flights STRICTLY AFTER the anchor (delta >= 0 — flights at
            # exact anchor time qualify as "after" since they ARE the
            # conflict moment, except the P2F itself which is filtered
            # by ``u != p2f_uid``). NO fallback to BEFORE — if the host
            # has fewer than K candidates after, displace whatever
            # exists and log the shortfall.
            after_candidates: list[tuple[int, str]] = []
            for u, sx in rstate.items():
                if sx["staff_id"] != host_id or u == p2f_uid:
                    continue
                f_cand = flight_by_uid.get(u)
                if f_cand is None or not _is_displaceable(f_cand):
                    continue
                cand_min = std_to_ops_day_minutes(
                    f_cand.std, f_cand.date, d_day,
                )
                delta = cand_min - anchor_min
                if delta < 0:
                    continue
                after_candidates.append((delta, u))
            after_candidates.sort()  # ascending — closest first

            if not after_candidates:
                summary.n_anchor_shortage += 1
                summary.log(
                    f"P2F flt {p2f_f.flt} ({label}, host "
                    f"{host_staff.name if host_staff else host_id}): "
                    "host has no displaceable flight AFTER the anchor — "
                    "nothing to remove"
                )
                continue

            n_to_remove = min(anchor_count, len(after_candidates))
            if n_to_remove < anchor_count:
                summary.n_anchor_shortage += 1
                summary.log(
                    f"P2F flt {p2f_f.flt} ({label}): asked for "
                    f"{anchor_count} removals, only {len(after_candidates)} "
                    f"candidate(s) after anchor — taking {n_to_remove}"
                )

            for _delta, best_uid in after_candidates[:n_to_remove]:
                # State could have changed since we built the list
                # (rare: a prior anchor in the same loop already moved
                # this flight). Re-check the staff_id binding.
                if rstate[best_uid]["staff_id"] != host_id:
                    continue
                displaced_f = flight_by_uid[best_uid]
                displaced_std_iso = displaced_f.std.isoformat(timespec="minutes")
                _displace_one(
                    best_uid, p2f_f, host_id, label, rstate,
                    flight_by_uid, staff_today, matrix, counts,
                    staff_stds, excluded, p2f_handlers,
                    d_day, summary,
                )
                if rstate[best_uid]["staff_id"] != host_id:
                    removed_for_this_p2f.append(
                        (str(displaced_f.flt), displaced_std_iso),
                    )
                    if host_id != owner_id:
                        summary.note(
                            host_id,
                            f"P2F prep host: 1 flight removed for "
                            f"FLT {p2f_f.flt} {label} (owner was "
                            f"{rstate[p2f_uid]['staff_name']})",
                        )
                    else:
                        summary.note(
                            host_id,
                            f"Below target: 1 flight removed for "
                            f"P2F FLT {p2f_f.flt} {label}",
                        )

        # 2026-05-26 user direction: populate the P2F row's warning
        # column with the list of removed flights — just "FLT <no>@HH:MM".
        # This is only for P2F rows (other rows keep their existing
        # warning content). The warning column is where prep cost
        # surfaces.
        if removed_for_this_p2f:
            tag = ", ".join(
                f"FLT {flt}@{std}" for flt, std in removed_for_this_p2f
            )
            existing = rstate[p2f_uid].get("warning") or ""
            new_warn = (
                f"{existing}; Removed: {tag}"
                if existing else f"Removed: {tag}"
            )
            rstate[p2f_uid]["warning"] = new_warn

        # 2026-05-27 user direction: P2F rows DO carry pair-style
        # annotations now — but driven by host (not by the pair table).
        # planned_by = D-3 / D-1 host (prep BEFORE flight).
        # relieved_by = D+20 host (paperwork AFTER flight).
        # relabel_pair_columns no longer blanks P2F so these survive
        # to the output sheet.
        if pre_host_id is not None:
            rstate[p2f_uid]["planned_by_emp"] = pre_host_id
            rstate[p2f_uid]["planned_by_name"] = pre_host_name
        if post_host_id is not None:
            rstate[p2f_uid]["relieved_by_emp"] = post_host_id
            rstate[p2f_uid]["relieved_by_name"] = post_host_name

    # Rebuild rows from rstate — same loop as INTL post-pass.
    new_rows: list[AllocationRow] = []
    for uid, st in rstate.items():
        f = flight_by_uid.get(uid)
        if f is None:
            new_rows.append(st["row"])
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
            f"!! P2F post-pass row count mismatch: input={rows_input_count} "
            f"output={len(new_rows)}"
        )
    return new_rows, summary


def _displace_one(
    displaced_uid: str,
    p2f_f: FlightInput,
    handler_id: str,
    anchor_label: str,
    rstate: dict[str, dict],
    flight_by_uid: dict[str, FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    counts: dict[str, int],
    staff_stds: dict[str, list[SpacingKey]],
    excluded: set[str],
    p2f_handlers: dict[ShiftCode, str],
    d_day: date_t,
    summary: P2FPostPassSummary,
) -> None:
    """Move one displaceable flight from the handler to a §4
    most-eligible recipient. If no recipient qualifies, leave the
    flight on the handler (no-op) and log the failure."""
    dst = rstate[displaced_uid]
    displaced_f = flight_by_uid[displaced_uid]
    displaced_entry = spacing_key(displaced_f, d_day)

    # Temporarily decrement the handler's count + remove the STD so
    # §4 sees an accurate post-removal state.
    counts[handler_id] = max(0, counts.get(handler_id, 0) - 1)
    if handler_id in staff_stds:
        with contextlib.suppress(ValueError):
            staff_stds[handler_id].remove(displaced_entry)

    # Exclude the handler so the same person doesn't take their own
    # flight back.
    excluded_for_pick = excluded | {handler_id}
    new_holder = _most_eligible(
        displaced_f, staff_today, matrix, counts, excluded_for_pick,
        preferred_shift=None,  # any qualifying shift
        p2f_handlers=p2f_handlers,
        staff_stds=staff_stds, flight_key=displaced_entry,
    )
    if new_holder is None:
        # Restore — keep the flight on the handler.
        counts[handler_id] += 1
        staff_stds.setdefault(handler_id, []).append(displaced_entry)
        summary.n_redistribute_failed += 1
        summary.log(
            f"P2F flt {p2f_f.flt} ({anchor_label}): could not redistribute "
            f"flt {displaced_f.flt} from {rstate[displaced_uid]['staff_name']}"
        )
        return

    # Commit the reassignment.
    counts[new_holder.employee_id] = counts.get(new_holder.employee_id, 0) + 1
    staff_stds.setdefault(new_holder.employee_id, []).append(displaced_entry)
    dst["staff_id"] = new_holder.employee_id
    dst["staff_name"] = new_holder.name
    dst["sheet_target"] = sheet_target_for(displaced_f, new_holder)
    excluded.add(new_holder.employee_id)
    summary.n_removed += 1
    summary.n_redistributed += 1
    summary.log(
        f"P2F flt {p2f_f.flt} ({anchor_label}): removed flt "
        f"{displaced_f.flt} from handler -> "
        f"{new_holder.name} ({new_holder.shift_today})"
    )
