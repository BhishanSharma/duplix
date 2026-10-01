"""Relievers for everyone but N and A1 staff, and early release when
there aren't enough of them (2026-10-01).

The rule
--------
Every on-shift person except N and A1 gets ONE reliever from the shift
that takes over from theirs::

    M  -> A      M1 -> A1      A  -> N

* A reliever relieves at most ONE person. (Before, an N staff member
  could end up named as reliever for three different A / A1 people.)
* Roles stay separate: ZC is relieved by ZC, STAFF by STAFF.
* N and A1 staff have no reliever and need none (nobody relieves
  them, and they are never released early).

When a shift has more people than relievers (typically A + A1 vs. the
smaller N), the people left over get NO reliever. Instead they are
released early: every flight they hold is moved off the last
``free_early_minutes`` (60) of their shift, so they finish their work
before shift end rather than leaving it for somebody else.

Fairness: who gets released early rotates. Candidates are ranked by how
often they were released over the last 30 days (``relief_store``), then
by a per-day pseudo-random key, so it is never the same person day
after day.

Flights are moved off a released person the same way the break pass
moves them: a 1-for-1 swap where possible (counts, and so the workload
spread, stay put), otherwise a plain transfer to someone eligible with
room. Anything that cannot be cleared (P2F / INTL flights are never
moved) is rolled back and that person is relieved instead; if there is
no reliever left either they are listed in a warning.

Runs after the INTL / P2F / rebalance post-passes and BEFORE the break
pass, which is told not to hand released people late flights again.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_t

from ..schemas import (
    AllocationRow,
    FlightInput,
    OpsClass,
    Pair,
    Role,
    ShiftCode,
    StaffMember,
)
from .caps import acceptable_max_for, hard_cap_for
from .eligibility import sheet_target_for
from .windows import (
    SHIFT_NOMINAL_MIN,
    SpacingKey,
    spacing_clear,
    spacing_key,
    std_to_ops_day_minutes,
)

# Who relieves whom. N and A1 are deliberately absent: neither has a
# reliever (and so neither is ever released early).
RELIEF_SHIFT: dict[str, str] = {"M": "A", "M1": "A1", "A": "N"}

FREE_EARLY_MIN_DEFAULT = 60
# A flight needs the handler until ~STD+20 (post-airborne work), so a
# flight departing within this many minutes of shift end is "handed over".
HANDOVER_LEAD_MIN = 20


def row_uid(r: AllocationRow) -> str:
    return (
        f"{r.flt}|{r.dep}|{r.arr}|{r.std.isoformat(timespec='minutes')}"
        f"|{r.date.isoformat()}"
    )


def _fmt(ops_min: int) -> str:
    day_offset, m = divmod(ops_min, 1440)
    h, mm = divmod(m, 60)
    return f"{h:02d}:{mm:02d}" + (" (+1d)" if day_offset else "")


@dataclass
class ReliefPlan:
    reliever_of: dict[str, str] = field(default_factory=dict)      # staff -> reliever id
    reliever_name: dict[str, str] = field(default_factory=dict)    # staff -> reliever name
    freed: dict[str, int] = field(default_factory=dict)            # staff -> cutoff (ops min)
    uncovered: list[str] = field(default_factory=list)             # no reliever AND not freed
    n_swaps: int = 0
    n_transfers: int = 0
    workload_notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    log_lines: list[str] = field(default_factory=list)

    def note(self, staff_id: str, msg: str) -> None:
        self.workload_notes[staff_id].append(msg)


def _day_key(employee_id: str, day: date_t) -> int:
    h = hashlib.md5(f"{employee_id}|{day.isoformat()}".encode()).hexdigest()
    return int(h[:8], 16)


def plan_relief(
    rows: list[AllocationRow],
    flights: list[FlightInput],
    staff_today: list[StaffMember],
    matrix: dict[str, set[str]],
    d_day: date_t,
    pairs: list[Pair],
    *,
    free_early_min: int = FREE_EARLY_MIN_DEFAULT,
    history: dict[str, int] | None = None,
    move_flights: bool = True,
) -> tuple[list[AllocationRow], ReliefPlan]:
    """Assign relievers, release the surplus early, return updated rows.

    ``move_flights=False`` (the pinned re-solve) only decides who is
    relieved / released and never reassigns a flight.
    """
    history = history or {}
    plan = ReliefPlan()
    flight_by_uid = {f.unique_id: f for f in flights}
    staff_by_id = {s.employee_id: s for s in staff_today}

    # ---- mutable allocation state ------------------------------------
    assign: dict[str, str] = {}
    keys: dict[str, dict[str, SpacingKey]] = defaultdict(dict)
    count: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r.staff_employee_id:
            continue
        uid = row_uid(r)
        assign[uid] = r.staff_employee_id
        count[r.staff_employee_id] += 1
        f = flight_by_uid.get(uid)
        if f is not None:
            keys[r.staff_employee_id][uid] = spacing_key(f, d_day)
    moved_names: dict[str, str] = {}

    def f_min(uid: str) -> int:
        f = flight_by_uid[uid]
        return std_to_ops_day_minutes(f.std, f.date, d_day)

    def movable(uid: str) -> bool:
        f = flight_by_uid.get(uid)
        return f is not None and f.ops_class != OpsClass.P2F and not f.is_international

    def cutoff_for(s: StaffMember) -> int | None:
        nominal = SHIFT_NOMINAL_MIN.get(s.shift_today) if s.shift_today else None
        return None if nominal is None else nominal[1] - free_early_min

    def late_uids(sid: str, cutoff: int) -> list[str]:
        return sorted(
            (u for u in keys.get(sid, {}) if f_min(u) > cutoff),
            key=f_min, reverse=True,
        )

    journal: list[tuple[str, str]] = []

    def move(uid: str, to_sid: str) -> None:
        old = assign[uid]
        journal.append((uid, old))
        k = keys[old].pop(uid, None)
        count[old] -= 1
        assign[uid] = to_sid
        if k is not None:
            keys[to_sid][uid] = k
        count[to_sid] += 1

    def rollback() -> None:
        while journal:
            uid, old = journal.pop()
            cur = assign[uid]
            k = keys[cur].pop(uid, None)
            count[cur] -= 1
            assign[uid] = old
            if k is not None:
                keys[old][uid] = k
            count[old] += 1

    freed_cutoff: dict[str, int] = {}

    def recipient_ok(rid: str, uid: str) -> bool:
        p = staff_by_id.get(rid)
        if p is None or p.role == Role.AM or p.shift_today is None:
            return False
        cut = freed_cutoff.get(rid)
        return not (cut is not None and f_min(uid) > cut)

    def try_clear(s: StaffMember, cutoff: int) -> tuple[bool, int, int]:
        """Move every flight after ``cutoff`` off ``s``. All-or-nothing."""
        sid = s.employee_id
        swaps = transfers = 0
        journal.clear()
        late = late_uids(sid, cutoff)
        leaving = set(late)
        for uid in late:
            if not movable(uid):
                rollback()
                return False, 0, 0
        for uid in late:
            f = flight_by_uid[uid]
            elig = sorted(matrix.get(uid, ()))
            cands = [
                rid for rid in elig
                if rid != sid and recipient_ok(rid, uid)
            ]
            cands.sort(key=lambda rid: (
                staff_by_id[rid].shift_today != s.shift_today,
                count[rid], rid,
            ))
            done = False
            # 1) 1-for-1 swap — keeps both counts.
            for rid in cands:
                for g in sorted(keys[rid], key=f_min):
                    if g in leaving or not movable(g) or f_min(g) > cutoff:
                        continue
                    if sid not in matrix.get(g, ()):
                        continue
                    g_key = keys[rid][g]
                    s_keep = [k for u, k in keys[sid].items() if u not in leaving]
                    if not spacing_clear(g_key, s_keep):
                        continue
                    r_keep = [k for u, k in keys[rid].items() if u != g]
                    if not spacing_clear(spacing_key(f, d_day), r_keep):
                        continue
                    move(uid, rid)
                    move(g, sid)
                    swaps += 1
                    done = True
                    break
                if done:
                    break
            if done:
                continue
            # 2) plain transfer to someone with room (hard cap is a wall;
            #    above the acceptable max is a last resort).
            f_key = spacing_key(f, d_day)
            room = [
                rid for rid in cands
                if count[rid] + 1 <= hard_cap_for(staff_by_id[rid])
                and spacing_clear(f_key, keys[rid].values())
            ]
            room.sort(key=lambda rid: (
                count[rid] + 1 > acceptable_max_for(staff_by_id[rid]),
                staff_by_id[rid].shift_today != s.shift_today,
                count[rid], rid,
            ))
            if not room:
                rollback()
                return False, 0, 0
            move(uid, room[0])
            transfers += 1
        journal.clear()
        return True, swaps, transfers

    # ---- who needs a reliever, who can be one ------------------------
    needs: dict[tuple[str, Role], list[StaffMember]] = defaultdict(list)   # (relief shift, role)
    pool: dict[tuple[str, Role], list[StaffMember]] = defaultdict(list)
    for s in staff_today:
        if s.role == Role.AM or s.shift_today is None:
            continue
        if s.shift_today in RELIEF_SHIFT:
            needs[(RELIEF_SHIFT[s.shift_today], s.role)].append(s)
        pool[(s.shift_today, s.role)].append(s)

    partner_pref: dict[str, list[str]] = defaultdict(list)
    for p in pairs:
        if p.prev_employee_id and p.next_employee_id:
            partner_pref[p.prev_employee_id].append(p.next_employee_id)

    for (relief_shift, role), cands in sorted(
        needs.items(), key=lambda kv: (kv[0][0], kv[0][1].value),
    ):
        relievers = sorted(pool.get((relief_shift, role), []), key=lambda x: x.name)
        surplus = max(0, len(cands) - len(relievers))

        # Hardest-to-release last: anyone holding P2F / INTL flights in
        # their final hour can't be cleared.
        def unfreeable(s: StaffMember) -> bool:
            cut = cutoff_for(s)
            return cut is not None and any(
                not movable(u) for u in late_uids(s.employee_id, cut)
            )

        order = sorted(
            cands,
            key=lambda s: (
                unfreeable(s),
                history.get(s.employee_id, 0),
                _day_key(s.employee_id, d_day),
                s.employee_id,
            ),
        )

        freed_here: list[StaffMember] = []
        for s in order:
            if len(freed_here) >= surplus:
                break
            cut = cutoff_for(s)
            if cut is None:
                continue
            if move_flights:
                ok, sw, tr = try_clear(s, cut)
            else:
                ok, sw, tr = (not late_uids(s.employee_id, cut)), 0, 0
            if not ok:
                plan.log_lines.append(
                    f"{s.shift_today}/{role.value}: could not clear {s.name}'s "
                    f"last {free_early_min} min — relieving them instead"
                )
                continue
            freed_cutoff[s.employee_id] = cut
            freed_here.append(s)
            plan.n_swaps += sw
            plan.n_transfers += tr

        freed_ids = {s.employee_id for s in freed_here}
        plan.freed.update({s.employee_id: freed_cutoff[s.employee_id] for s in freed_here})

        # Everyone not released gets a reliever (each reliever used once).
        # Those who were hardest to release are matched first.
        to_match = [s for s in reversed(order) if s.employee_id not in freed_ids]
        used: set[str] = set()
        for s in sorted(to_match, key=lambda x: x.name):
            pick = None
            for nid in partner_pref.get(s.employee_id, ()):
                rel = staff_by_id.get(nid)
                if (rel and nid not in used and rel.shift_today == relief_shift
                        and rel.role == role):
                    pick = rel
                    break
            if pick is None:
                pick = next((r for r in relievers if r.employee_id not in used), None)
            if pick is None:
                plan.uncovered.append(s.employee_id)
                continue
            used.add(pick.employee_id)
            plan.reliever_of[s.employee_id] = pick.employee_id
            plan.reliever_name[s.employee_id] = pick.name

        # Last resort for anyone still without a reliever whose full
        # window couldn't be cleared: release them as early as we can
        # (shorter window) instead of leaving them uncovered.
        if move_flights:
            for sid in list(plan.uncovered):
                s = staff_by_id[sid]
                if s.shift_today is None or (RELIEF_SHIFT.get(s.shift_today), s.role) != (relief_shift, role):
                    continue
                end = SHIFT_NOMINAL_MIN[s.shift_today][1]
                for mins in (free_early_min * 3 // 4, free_early_min // 2, free_early_min // 4):
                    if mins < 10:
                        continue
                    ok, sw, tr = try_clear(s, end - mins)
                    if ok:
                        freed_cutoff[sid] = end - mins
                        plan.freed[sid] = end - mins
                        plan.uncovered.remove(sid)
                        plan.n_swaps += sw
                        plan.n_transfers += tr
                        plan.log_lines.append(
                            f"{s.name}: only {mins} min early release possible"
                        )
                        break

    # ---- notes + rows -------------------------------------------------
    for sid, rid in plan.reliever_of.items():
        plan.note(sid, f"Reliever: {plan.reliever_name[sid]}")
        plan.note(rid, f"Relieves: {staff_by_id[sid].name}")
    for sid, cut in plan.freed.items():
        s = staff_by_id[sid]
        end = SHIFT_NOMINAL_MIN[s.shift_today][1]
        plan.note(
            sid,
            f"No reliever available — released early: no flights after "
            f"{_fmt(cut)} (shift ends {_fmt(end)})",
        )
    for sid in plan.uncovered:
        plan.note(sid, "No reliever and could not be released early")
    for sid in plan.freed:
        plan.log_lines.append(f"released early: {staff_by_id[sid].name}")

    new_rows: list[AllocationRow] = []
    for r in rows:
        uid = row_uid(r)
        new_sid = assign.get(uid)
        if not r.staff_employee_id or new_sid is None or new_sid == r.staff_employee_id:
            new_rows.append(r)
            continue
        rec = staff_by_id[new_sid]
        f = flight_by_uid[uid]
        new_rows.append(r.model_copy(update={
            "staff_employee_id": rec.employee_id,
            "staff_name": rec.name,
            "sheet_target": sheet_target_for(f, rec),
        }))
    return new_rows, plan


def relief_window_start(shift: ShiftCode) -> int:
    """Ops-day minute from which a flight counts as handed to the reliever."""
    return SHIFT_NOMINAL_MIN[shift][1] - HANDOVER_LEAD_MIN
