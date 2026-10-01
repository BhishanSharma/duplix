"""Floating-break post-pass (2026-09-23, soft / best-effort).

Gives each on-shift staff member a floating break somewhere mid-shift
(``break_pass.length_minutes`` in config.yml, 30 min by default),
WITHOUT changing anyone's flight count — it only performs 1-for-1
SWAPS between staff in the same (shift, role) bucket. Because
a swap is always "I give you one flight, you give me one flight",
both people's totals are unchanged, so the workload spread that H18 /
``postpass_rebalance`` already achieved is left completely intact.

Deliberately NOT built into the CP-SAT model: that would need a new
break-window variable plus break/flight overlap bookkeeping for
essentially every (flight, staff) pair in the eligibility matrix —
tens of thousands of new variables and constraints on top of a solve
that already runs ~400-700s. The risk is the solve slows down enough
to threaten the very H18 spread this pass exists to protect. A
lightweight post-pass swap gets the same practical outcome (everyone
gets a real, contiguous, no-flight window) at a fraction of the cost,
with the trade-off that it's best-effort: if no eligible swap partner
exists for a flight sitting in someone's best available window, that
flight is left in place and the break is reported as partial/absent
rather than forced.

Order of operations in the pipeline matters: this runs LAST, after
P2F, INTL, and the workload rebalance pass, so it's swapping against
the final, already-balanced schedule.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_t

from ..config import BREAK_MINUTES_DEFAULT
from ..schemas import (
    AllocationRow,
    FlightInput,
    OpsClass,
    Role,
    ShiftCode,
    StaffMember,
)
from .eligibility import sheet_target_for
from .windows import (
    SHIFT_NOMINAL_MIN,
    SpacingKey,
    spacing_clear,
    spacing_key,
    std_to_ops_day_minutes,
)

# Default only — the Allocate run passes the configured length.
BREAK_LEN_MIN = BREAK_MINUTES_DEFAULT
# Keep the break out of the first/last hour of the shift so it reads as
# a genuine mid-shift break, not a shift-boundary artifact.
EDGE_MARGIN_MIN = 60


def _fmt(ops_min: int) -> str:
    day_offset, m = divmod(ops_min, 1440)
    h, mm = divmod(m, 60)
    suffix = " (+1d)" if day_offset else ""
    return f"{h:02d}:{mm:02d}{suffix}"


@dataclass
class BreakSummary:
    n_staff_considered: int = 0
    n_clean: int = 0            # already had a natural break-length gap
    n_created_via_swap: int = 0  # fully cleared via 1+ swaps
    n_partial: int = 0          # some conflicts swapped, some left
    n_not_found: int = 0        # no window could be cleared at all
    n_swaps: int = 0
    workload_notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    log_lines: list[str] = field(default_factory=list)

    def note(self, staff_id: str, reason: str) -> None:
        self.workload_notes[staff_id].append(reason)

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)


def _transferable(flight: FlightInput | None) -> bool:
    """Only plain-domestic flights are swap candidates — P2F routing
    is dedicated to specific handlers and isn't touched here."""
    if flight is None:
        return False
    return flight.ops_class != OpsClass.P2F


def _best_window(
    stds: list[int], lo: int, hi: int, break_len: int,
) -> tuple[int, list[int]] | None:
    """Find the break_len-minute window inside [lo, hi] that overlaps
    the fewest of ``stds`` (a staff member's currently-assigned STDs,
    in ops-day minutes). Returns (window_start, conflicting_stds), or
    None if [lo, hi] is too narrow to fit a window at all.

    Candidate starts: the range floor, the range ceiling minus the
    break length, and "right after each existing flight" — cheap and
    sufficient since we only need the true minimum, not every window.
    """
    if hi - lo < break_len:
        return None
    candidates = {lo, hi - break_len}
    for s in stds:
        c = s + 1
        if lo <= c <= hi - break_len:
            candidates.add(c)
    best: tuple[int, list[int]] | None = None
    for c in sorted(candidates):
        conflicts = [s for s in stds if c <= s < c + break_len]
        if best is None or len(conflicts) < len(best[1]):
            best = (c, conflicts)
        if best[1] == []:
            break
    return best


def apply_break_pass(
    rows: list[AllocationRow],
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    d_day: date_t,
    break_len: int = BREAK_LEN_MIN,
    edge_margin: int = EDGE_MARGIN_MIN,
    max_std_by_staff: dict[str, int] | None = None,
) -> tuple[list[AllocationRow], BreakSummary]:
    """``max_std_by_staff``: staff released early (no reliever) mapped
    to the latest ops-day minute they may hold a flight; a swap never
    hands them a later one."""
    summary = BreakSummary()
    _max_std = max_std_by_staff or {}
    flight_by_uid = {f.unique_id: f for f in flights}
    staff_by_id = {s.employee_id: s for s in staff_today}

    rstate: dict[str, dict] = {}
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
        }

    # Per-staff SpacingKey list + a quick uid lookup by (staff, std).
    staff_stds: dict[str, list[SpacingKey]] = defaultdict(list)
    uid_by_staff_std: dict[tuple[str, int], list[str]] = defaultdict(list)
    for uid, st in rstate.items():
        if not st["staff_id"]:
            continue
        f_ref = flight_by_uid.get(uid)
        if f_ref is None:
            continue
        key = spacing_key(f_ref, d_day)
        m = key[0]
        staff_stds[st["staff_id"]].append(key)
        uid_by_staff_std[(st["staff_id"], m)].append(uid)

    buckets: dict[tuple[ShiftCode, Role], list[StaffMember]] = defaultdict(list)
    for s in staff_today:
        if s.shift_today is None or s.role == Role.AM:
            continue
        buckets[(s.shift_today, s.role)].append(s)

    assigned_break: dict[str, tuple[int, int]] = {}

    def _spacing_ok(
        staff_id: str, new_key: SpacingKey, skip_std: int | None = None,
    ) -> bool:
        return spacing_clear(new_key, (
            k for k in staff_stds.get(staff_id, ())
            if skip_std is None or k[0] != skip_std
        ))

    def _during_break(staff_id: str, std: int) -> bool:
        win = assigned_break.get(staff_id)
        return win is not None and win[0] <= std < win[1]

    def _do_swap(uid_a: str, staff_a: str, uid_b: str, staff_b: str) -> None:
        """Give ``uid_a``'s flight to ``staff_b`` and ``uid_b``'s to
        ``staff_a``. Keeps both counts, staff_stds, and rstate in sync."""
        st_a, st_b = rstate[uid_a], rstate[uid_b]
        f_a, f_b = flight_by_uid[uid_a], flight_by_uid[uid_b]
        std_a = std_to_ops_day_minutes(f_a.std, f_a.date, d_day)
        std_b = std_to_ops_day_minutes(f_b.std, f_b.date, d_day)
        recip_a = staff_by_id[staff_b]  # receives uid_a
        recip_b = staff_by_id[staff_a]  # receives uid_b

        entry_a = spacing_key(f_a, d_day)
        entry_b = spacing_key(f_b, d_day)
        with contextlib.suppress(ValueError):
            staff_stds[staff_a].remove(entry_a)
        with contextlib.suppress(ValueError):
            staff_stds[staff_b].remove(entry_b)
        staff_stds[staff_a].append(entry_b)
        staff_stds[staff_b].append(entry_a)

        st_a["staff_id"], st_a["staff_name"] = recip_a.employee_id, recip_a.name
        st_a["sheet_target"] = sheet_target_for(f_a, recip_a)
        st_b["staff_id"], st_b["staff_name"] = recip_b.employee_id, recip_b.name
        st_b["sheet_target"] = sheet_target_for(f_b, recip_b)

    for (shift_code, role), bucket in sorted(
        buckets.items(), key=lambda kv: (kv[0][0], kv[0][1].value),
    ):
        nominal = SHIFT_NOMINAL_MIN.get(shift_code)
        if nominal is None:
            continue
        lo = nominal[0] + edge_margin
        hi = nominal[1] - edge_margin
        for s in sorted(bucket, key=lambda x: x.name):
            summary.n_staff_considered += 1
            window = _best_window(
                [k[0] for k in staff_stds.get(s.employee_id, [])], lo, hi, break_len,
            )
            if window is None:
                summary.n_not_found += 1
                summary.log(
                    f"{s.name} ({shift_code}/{role.value}): shift too "
                    f"short for a {break_len}-min mid-shift window "
                    f"(margin={edge_margin})"
                )
                continue
            win_start, conflicts = window
            win_end = win_start + break_len

            if not conflicts:
                assigned_break[s.employee_id] = (win_start, win_end)
                summary.n_clean += 1
                summary.note(
                    s.employee_id,
                    f"Break: {_fmt(win_start)}-{_fmt(win_end)} (already clear)",
                )
                continue

            resolved: list[int] = []
            unresolved: list[int] = []
            for conflict_std in conflicts:
                donor_uids = uid_by_staff_std.get((s.employee_id, conflict_std), [])
                donor_uid = next(
                    (u for u in donor_uids if _transferable(flight_by_uid.get(u))),
                    None,
                )
                if donor_uid is None:
                    unresolved.append(conflict_std)
                    continue
                f = flight_by_uid[donor_uid]
                eligible_for_f = matrix.get(donor_uid, set())

                swapped = False
                for peer in sorted(bucket, key=lambda x: x.name):
                    if peer.employee_id == s.employee_id:
                        continue
                    if peer.employee_id not in eligible_for_f:
                        continue
                    if _during_break(peer.employee_id, conflict_std):
                        continue
                    if conflict_std > _max_std.get(peer.employee_id, 10**9):
                        continue
                    if not _spacing_ok(peer.employee_id, spacing_key(f, d_day)):
                        continue
                    # Find a plain-domestic flight of peer's, outside s's
                    # break window, that s is eligible for and that fits
                    # s's schedule once the conflict flight is removed.
                    peer_uids = sorted(
                        (
                            u for u, st in rstate.items()
                            if st["staff_id"] == peer.employee_id
                            and _transferable(flight_by_uid.get(u))
                        ),
                        key=lambda u: std_to_ops_day_minutes(
                            flight_by_uid[u].std, flight_by_uid[u].date, d_day,
                        ),
                    )
                    for g_uid in peer_uids:
                        g = flight_by_uid[g_uid]
                        g_std = std_to_ops_day_minutes(g.std, g.date, d_day)
                        if win_start <= g_std < win_end:
                            continue  # would just create a new conflict
                        if g_std > _max_std.get(s.employee_id, 10**9):
                            continue  # s was released early
                        if s.employee_id not in matrix.get(g_uid, set()):
                            continue
                        if not _spacing_ok(
                            s.employee_id, spacing_key(g, d_day),
                            skip_std=conflict_std,
                        ):
                            continue
                        _do_swap(donor_uid, s.employee_id, g_uid, peer.employee_id)
                        summary.n_swaps += 1
                        summary.note(
                            s.employee_id,
                            f"Break: swapped FLT {f.flt} to {peer.name} "
                            f"for FLT {g.flt}, to clear "
                            f"{_fmt(win_start)}-{_fmt(win_end)} break",
                        )
                        summary.note(
                            peer.employee_id,
                            f"Break: swapped FLT {g.flt} to {s.name} "
                            f"for FLT {f.flt}, to help their "
                            f"{_fmt(win_start)}-{_fmt(win_end)} break",
                        )
                        summary.log(
                            f"{shift_code}/{role.value}: break-swap "
                            f"{s.name}<->{peer.name}: {f.flt}<->{g.flt}"
                        )
                        swapped = True
                        break
                    if swapped:
                        break
                if swapped:
                    resolved.append(conflict_std)
                else:
                    unresolved.append(conflict_std)

            if not unresolved:
                assigned_break[s.employee_id] = (win_start, win_end)
                summary.n_created_via_swap += 1
                summary.note(
                    s.employee_id,
                    f"Break: {_fmt(win_start)}-{_fmt(win_end)} "
                    f"(cleared via {len(resolved)} swap(s))",
                )
            elif resolved:
                summary.n_partial += 1
                summary.note(
                    s.employee_id,
                    f"Break: best window {_fmt(win_start)}-{_fmt(win_end)} "
                    f"still has {len(unresolved)} flight(s) — no eligible "
                    "swap partner found for the rest",
                )
            else:
                summary.n_not_found += 1
                summary.note(
                    s.employee_id,
                    f"Break: no eligible swap found — best window "
                    f"{_fmt(win_start)}-{_fmt(win_end)} still has "
                    f"{len(unresolved)} flight(s)",
                )

    # Rebuild the rows list from rstate (AllocationRow is frozen).
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
    return new_rows, summary
