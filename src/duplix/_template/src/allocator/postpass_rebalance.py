"""Final workload rebalance pass (2026-09-23 fix).

The P2F and INTL post-passes (``postpass_p2f.py`` / ``postpass_intl.py``)
run AFTER the CP-SAT solver, and each one removes flights from whichever
staff happen to be adjacent to a P2F/INTL flight — not from whoever
currently has the most flights. The solver's own within-bucket balancing
(H18, ``solver/allocator_cpsat.py``) has already run by then, so those
post-pass removals can re-open a spread the solver had just closed: one
person loses two flights because they happened to work two INTL
departures back-to-back, another loses none, and the bucket ends up 3-4
flights apart instead of the intended <=1-2 (see docs/REVIEW_NOTES.md
finding #4).

This pass runs LAST, after both post-passes, and does the obvious thing
a human scheduler would do: for each (shift, role) bucket (same
exclusions as H18 — AM role and P2F/NORSE handlers are left out, since
their workload mix isn't comparable), while the most-loaded and
least-loaded staff differ by more than ``target_spread``, move ONE
eligible plain-domestic flight from the most-loaded to the
least-loaded staff member. Repeat until the bucket is within target or
no further eligible transfer exists (matrix eligibility, H10 spacing,
and hard cap are all still enforced — this never creates a new
constraint violation, it only chooses who ends up doing which of the
flights that were already validly assignable to both people).
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
from .windows import required_spacing_min, std_to_ops_day_minutes


@dataclass
class RebalanceSummary:
    n_buckets_checked: int = 0
    n_buckets_improved: int = 0
    n_buckets_stuck: int = 0
    n_transfers: int = 0
    workload_notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    log_lines: list[str] = field(default_factory=list)

    def note(self, staff_id: str, reason: str) -> None:
        self.workload_notes[staff_id].append(reason)

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)


def _transferable(flight: FlightInput | None) -> bool:
    """Only move plain-domestic flights. P2F/NORSE routing is handled
    by dedicated logic elsewhere and shouldn't be touched here — this
    mirrors ``postpass_intl._eligible_for_removal``."""
    if flight is None:
        return False
    return flight.ops_class not in (OpsClass.P2F, OpsClass.NORSE)


def apply_rebalance_pass(
    rows: list[AllocationRow],
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    p2f_handlers: dict[ShiftCode, str],
    norse_handlers: set[str],
    d_day: date_t,
    target_spread: int = 1,
    max_transfers_per_bucket: int = 40,
) -> tuple[list[AllocationRow], RebalanceSummary]:
    """Level workload within each (shift, role) bucket by transferring
    flights from the most- to the least-loaded staff. Returns updated
    rows and a summary the orchestrator can fold into the workload
    notes (same shape as the P2F/INTL post-pass summaries).
    """
    summary = RebalanceSummary()
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

    counts: dict[str, int] = defaultdict(int)
    staff_stds: dict[str, list[int]] = defaultdict(list)
    for uid, st in rstate.items():
        if not st["staff_id"]:
            continue
        counts[st["staff_id"]] += 1
        f_ref = flight_by_uid.get(uid)
        if f_ref is not None:
            staff_stds[st["staff_id"]].append(
                std_to_ops_day_minutes(f_ref.std, f_ref.date, d_day)
            )

    excluded_handlers = set(p2f_handlers.values()) | set(norse_handlers)
    buckets: dict[tuple[ShiftCode, Role], list[StaffMember]] = defaultdict(list)
    for s in staff_today:
        if s.shift_today is None or s.role == Role.AM:
            continue
        if s.employee_id in excluded_handlers:
            continue
        buckets[(s.shift_today, s.role)].append(s)

    for (shift_code, role), bucket in buckets.items():
        if len(bucket) < 2:
            continue
        summary.n_buckets_checked += 1
        bucket_ids = [s.employee_id for s in bucket]
        n_transfers_this_bucket = 0
        improved = False
        while n_transfers_this_bucket < max_transfers_per_bucket:
            bucket_counts = {eid: counts.get(eid, 0) for eid in bucket_ids}
            max_c = max(bucket_counts.values())
            min_c = min(bucket_counts.values())
            if max_c - min_c <= target_spread:
                break
            donors = sorted(eid for eid, c in bucket_counts.items() if c == max_c)
            recipients = sorted(eid for eid, c in bucket_counts.items() if c == min_c)

            transferred = False
            for donor_id in donors:
                donor = staff_by_id[donor_id]
                donor_flight_uids = sorted(
                    (
                        uid for uid, st in rstate.items()
                        if st["staff_id"] == donor_id
                        and _transferable(flight_by_uid.get(uid))
                    ),
                    key=lambda u: std_to_ops_day_minutes(
                        flight_by_uid[u].std, flight_by_uid[u].date, d_day,
                    ),
                )
                for uid in donor_flight_uids:
                    f = flight_by_uid[uid]
                    f_std_min = std_to_ops_day_minutes(f.std, f.date, d_day)
                    eligible_set = matrix.get(uid, set())
                    for recipient_id in recipients:
                        if recipient_id == donor_id:
                            continue
                        if recipient_id not in eligible_set:
                            continue
                        recipient = staff_by_id[recipient_id]
                        if counts.get(recipient_id, 0) >= hard_cap_for(recipient):
                            continue
                        existing = staff_stds.get(recipient_id, ())
                        if not all(
                            abs(std - f_std_min) >= required_spacing_min(std, f_std_min)
                            for std in existing
                        ):
                            continue

                        # -- transfer donor -> recipient --
                        st = rstate[uid]
                        counts[donor_id] -= 1
                        counts[recipient_id] = counts.get(recipient_id, 0) + 1
                        with contextlib.suppress(ValueError):
                            staff_stds[donor_id].remove(f_std_min)
                        staff_stds.setdefault(recipient_id, []).append(f_std_min)
                        st["staff_id"] = recipient.employee_id
                        st["staff_name"] = recipient.name
                        st["sheet_target"] = sheet_target_for(f, recipient)

                        summary.n_transfers += 1
                        n_transfers_this_bucket += 1
                        summary.note(
                            donor_id,
                            f"Rebalance: gave up FLT {f.flt} to "
                            f"{recipient.name} to level {shift_code}/"
                            f"{role.value} workload",
                        )
                        summary.note(
                            recipient_id,
                            f"Rebalance: received FLT {f.flt} from "
                            f"{donor.name} to level {shift_code}/"
                            f"{role.value} workload",
                        )
                        summary.log(
                            f"{shift_code}/{role.value}: moved flt {f.flt} "
                            f"{donor.name} ({bucket_counts[donor_id]}) -> "
                            f"{recipient.name} ({bucket_counts[recipient_id]})"
                        )
                        transferred = True
                        improved = True
                        break
                    if transferred:
                        break
                if transferred:
                    break

            if not transferred:
                summary.n_buckets_stuck += 1
                summary.log(
                    f"{shift_code}/{role.value}: spread still "
                    f"{max_c - min_c} after {n_transfers_this_bucket} "
                    "transfer(s) — no further eligible transfer found "
                    "(matrix / H10 spacing / hard cap all blocked it)"
                )
                break
        if improved:
            summary.n_buckets_improved += 1

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
