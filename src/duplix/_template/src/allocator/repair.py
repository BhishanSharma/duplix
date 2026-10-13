"""Repair pass: place leftover flights with short chains of swaps.

Runs after every other pass and BEFORE force-place. A flight the solver
left unplaced usually has eligible people on shift — they are just busy
with another flight a few minutes away. This pass tries to free one of
them: give the leftover flight to person A, and move A's clashing
flight(s) to person B (whose own clash may in turn move to person C, up
to ``max_depth`` hops). A chain is only kept when every step obeys the
hard rules, so nothing here relaxes anything; whatever it cannot place
still goes to force-place.

Every move checks the same things the rebalance pass does: the
eligibility matrix (shift window, P2F handler block, INTL D-75 coverage,
newbies, ZC cutoff), H10 spacing (15 min, 30 before an INTL departure),
the hard cap, early releases and secured break windows. P2F flights are
never moved, and a flight the P2F / INTL post-passes took off someone is
never handed back to them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_t

from ..schemas import FlightInput, OpsClass, Role, StaffMember
from .caps import hard_cap_for, preferred_target_for
from .windows import required_spacing_min, spacing_key, std_to_ops_day_minutes


@dataclass
class RepairSummary:
    placed: dict[str, str] = field(default_factory=dict)   # leftover uid -> staff
    moved: dict[str, str] = field(default_factory=dict)    # placed uid -> new staff
    n_tried: int = 0
    workload_notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    log_lines: list[str] = field(default_factory=list)


def repair_leftovers(
    leftovers: list[FlightInput],
    assignments: dict[str, str],
    all_flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    *,
    ops_day: date_t,
    max_std_by_staff: dict[str, int] | None = None,
    blocked_windows: dict[str, tuple[int, int]] | None = None,
    forbidden: set[tuple[str, str]] | frozenset = frozenset(),
    max_depth: int = 3,
    max_candidates: int = 8,
    node_budget: int = 40_000,
) -> RepairSummary:
    """Try to place each leftover flight without relaxing any rule.

    ``assignments`` is the current ``{uid: employee_id}``; it is not
    modified. ``forbidden`` holds ``(uid, employee_id)`` pairs that must
    not be created. Returns the placements and the moves they needed.
    """
    summary = RepairSummary()
    by_uid = {f.unique_id: f for f in all_flights}
    staff_by_id = {
        s.employee_id: s for s in staff_today
        if s.shift_today is not None and s.role != Role.AM
    }
    max_std = max_std_by_staff or {}
    blocked = blocked_windows or {}
    key = {uid: spacing_key(f, ops_day) for uid, f in by_uid.items()}
    minute = {
        uid: std_to_ops_day_minutes(f.std, f.date, ops_day)
        for uid, f in by_uid.items()
    }
    cap = {eid: hard_cap_for(s) for eid, s in staff_by_id.items()}
    target = {eid: preferred_target_for(s) or 22 for eid, s in staff_by_id.items()}

    owner: dict[str, str] = {}
    held: dict[str, set[str]] = defaultdict(set)
    for uid, eid in assignments.items():
        if uid in by_uid and eid:
            owner[uid] = eid
            held[eid].add(uid)
    original = dict(owner)

    journal: list[tuple[str, str | None]] = []   # (uid, previous owner)

    def _assign(uid: str, eid: str | None) -> None:
        journal.append((uid, owner.get(uid)))
        prev = owner.pop(uid, None)
        if prev is not None:
            held[prev].discard(uid)
        if eid is not None:
            owner[uid] = eid
            held[eid].add(uid)

    def _undo_to(mark: int) -> None:
        while len(journal) > mark:
            uid, prev = journal.pop()
            cur = owner.pop(uid, None)
            if cur is not None:
                held[cur].discard(uid)
            if prev is not None:
                owner[uid] = prev
                held[prev].add(uid)

    def _clashes(uid: str, eid: str) -> list[str]:
        k = key[uid]
        return [
            h for h in held[eid]
            if abs(k[0] - key[h][0]) < required_spacing_min(k, key[h])
        ]

    def _options(uid: str, banned: set[str]) -> list[tuple[int, int, str, list[str]]]:
        f_min = minute[uid]
        out = []
        for eid in matrix.get(uid, ()):
            if eid in banned or eid not in staff_by_id:
                continue
            if (uid, eid) in forbidden:
                continue
            if f_min > max_std.get(eid, 10**9):
                continue
            win = blocked.get(eid)
            if win is not None and win[0] <= f_min < win[1]:
                continue
            clash = _clashes(uid, eid)
            if any(by_uid[h].ops_class == OpsClass.P2F for h in clash):
                continue
            if len(held[eid]) - len(clash) + 1 > cap[eid]:
                continue
            out.append((len(clash), len(held[eid]) - target[eid], eid, clash))
        out.sort()
        return out[:max_candidates]

    budget = [node_budget]

    def _place(uid: str, depth: int, banned: set[str], chain: set[str]) -> bool:
        budget[0] -= 1
        if budget[0] <= 0:
            return False
        opts = _options(uid, banned)
        for n_clash, _, eid, clash in opts:
            if n_clash == 0:
                _assign(uid, eid)
                return True
        if depth == 0:
            return False
        for n_clash, _, eid, clash in opts:
            if n_clash > 2 or any(h in chain for h in clash):
                continue
            mark = len(journal)
            for h in clash:
                _assign(h, None)
            _assign(uid, eid)
            ok = all(
                _place(h, depth - 1, banned | {eid}, chain | {uid, *clash})
                for h in clash
            )
            if ok:
                return True
            _undo_to(mark)
            if budget[0] <= 0:
                return False
        return False

    # Hardest first: fewest eligible people, then chronological.
    order = sorted(
        (f for f in leftovers if matrix.get(f.unique_id)),
        key=lambda f: (len(matrix[f.unique_id]), minute[f.unique_id]),
    )
    for f in order:
        summary.n_tried += 1
        budget[0] = node_budget
        mark = len(journal)
        if not _place(f.unique_id, max_depth, set(), {f.unique_id}):
            _undo_to(mark)

    for uid, eid in owner.items():
        if uid not in original:
            summary.placed[uid] = eid
        elif original[uid] != eid:
            summary.moved[uid] = eid
    for uid, eid in summary.placed.items():
        f = by_uid[uid]
        summary.workload_notes[eid].append(
            f"Repair: took leftover FLT {f.flt} (freed by swaps, no rule relaxed)"
        )
        summary.log_lines.append(
            f"placed leftover flt {f.flt} {f.std.isoformat(timespec='minutes')} "
            f"on {staff_by_id[eid].name}"
        )
    for uid, eid in summary.moved.items():
        f = by_uid[uid]
        old = staff_by_id[original[uid]]
        summary.workload_notes[original[uid]].append(
            f"Repair: gave FLT {f.flt} to {staff_by_id[eid].name} to make room "
            "for an unplaced flight"
        )
        summary.workload_notes[eid].append(
            f"Repair: received FLT {f.flt} from {old.name} to make room "
            "for an unplaced flight"
        )
        summary.log_lines.append(
            f"moved flt {f.flt} {old.name} -> {staff_by_id[eid].name}"
        )
    return summary
