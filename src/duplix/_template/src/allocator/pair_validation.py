"""Pair-per-shift-change validation (user direction 2026-05-12, Task 3).

After pair generation, walk every (boundary, side, employee) triple and
flag the cases where a single person ends up with more than one distinct
partner at the same boundary. The only sanctioned exception is the
``A_TO_N`` split-1+2 pattern, where one Night staff legitimately has two
A-shift partners (primary count=2, secondary count=1) — that's part of
the round-3 design, not a discrepancy.

Output is a list of ``WarningRow`` entries (severity=WARN, code=W215)
so the existing OUT_Warnings pipeline + dashboard panel surface them
without any new sheet. Each warning describes the conflict in plain
English so the assigner can review:

    "Jai Sundrani is paired with both Deepak Kathiat and Joel Verghese
     at the M→A boundary (3 partner(s) total). Review for exception."

The validator is purely diagnostic — it never blocks allocation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_t

from ..schemas import OpsBoundary, Pair, Severity, WarningRow


def _is_a_to_n_split(pairs: list[Pair]) -> bool:
    """Return True when this ``A_TO_N`` group is the sanctioned split-1+2
    pattern: exactly 2 partners on the opposite side, preplan counts
    summing to 3 with one entry =2 and the other =1. Other A→N
    multi-partner shapes (e.g., 1+1+1 or 2+2) still flag as discrepancies.
    """
    if len(pairs) != 2:
        return False
    counts = sorted(p.preplan_count for p in pairs)
    return counts == [1, 2]


def _format_partners(pairs: list[Pair], side: str) -> list[str]:
    """``side`` = 'next' or 'prev' — the OPPOSITE side from the one
    we're checking. Returns deduped, sorted display strings."""
    seen: set[str] = set()
    out: list[str] = []
    for p in pairs:
        emp_id = p.next_employee_id if side == "next" else p.prev_employee_id
        name = p.next_name if side == "next" else p.prev_name
        if emp_id is None:
            continue
        key = f"{emp_id}|{name or ''}"
        if key in seen:
            continue
        seen.add(key)
        out.append(name or emp_id)
    out.sort()
    return out


def validate_pair_discrepancies(
    pairs: list[Pair], d_day: date_t,
) -> list[WarningRow]:
    """Identify staff who appear in multiple pairs at the same boundary.

    Returns one WarningRow per flagged staff. Empty list when every
    pair is 1-to-1 (or every multi-pair group is a sanctioned exception
    such as A_TO_N split-1+2).
    """
    # Group by (boundary, "prev"/"next", employee_id). Each group is the
    # list of pairs that mention this person on this side at this
    # boundary. Multi-entry groups are candidates for a discrepancy
    # warning.
    by_role: dict[tuple[OpsBoundary, str, str, str], list[Pair]] = defaultdict(list)
    for p in pairs:
        if p.prev_employee_id is not None:
            by_role[(p.boundary, "prev", p.prev_employee_id, p.prev_name or "")].append(p)
        if p.next_employee_id is not None:
            by_role[(p.boundary, "next", p.next_employee_id, p.next_name or "")].append(p)

    # Shared planners (2026-10-16): when the next shift is bigger, some
    # previous-shift people plan for two. Sanctioned up to the fair share.
    sizes: dict[OpsBoundary, tuple[set[str], set[str]]] = defaultdict(
        lambda: (set(), set()),
    )
    for p in pairs:
        if p.preplan_count > 0 and p.prev_employee_id and p.next_employee_id:
            sizes[p.boundary][0].add(p.prev_employee_id)
            sizes[p.boundary][1].add(p.next_employee_id)

    def _shared_planner(boundary: OpsBoundary, group: list[Pair]) -> bool:
        prev_ids, next_ids = sizes.get(boundary, (set(), set()))
        if not prev_ids or len(next_ids) <= len(prev_ids):
            return False
        fair = -(-len(next_ids) // len(prev_ids))       # ceil
        return len(group) <= fair and all(p.preplan_count > 0 for p in group)

    out: list[WarningRow] = []
    for (boundary, side, emp_id, emp_name), group in by_role.items():
        if len(group) <= 1:
            continue
        # M1 helping A is sanctioned (see ``m1_help_notes``).
        if boundary == OpsBoundary.M1_TO_A:
            continue
        # Sanctioned A→N split-1+2 exception.
        if boundary == OpsBoundary.A_TO_N and _is_a_to_n_split(group):
            continue
        if side == "prev" and _shared_planner(boundary, group):
            continue
        opposite_side = "next" if side == "prev" else "prev"
        partners = _format_partners(group, opposite_side)
        if len(partners) <= 1:
            continue  # All entries share the same partner — not a conflict.
        out.append(WarningRow(
            severity=Severity.WARN,
            code="W215",
            name=emp_name or emp_id,
            date=d_day,
            message=(
                f"{emp_name or emp_id} is paired with {len(partners)} "
                f"different partner(s) at the {boundary.value} boundary: "
                f"{', '.join(partners)}. Review for exception."
            ),
        ))
    # Stable order: by boundary then employee name for deterministic
    # rendering in OUT_Warnings + the dashboard panel.
    out.sort(key=lambda w: (str(w.code), str(w.name)))
    return out


def shared_planner_notes(pairs: list[Pair], d_day: date_t) -> list[WarningRow]:
    """One INFO line per boundary where previous-shift people plan the
    first flights of two next-shift people (next shift bigger)."""
    by_prev: dict[tuple[OpsBoundary, str], list[Pair]] = defaultdict(list)
    for p in pairs:
        if (p.preplan_count > 0 and p.prev_employee_id and p.next_employee_id
                and p.boundary != OpsBoundary.M1_TO_A):
            by_prev[(p.boundary, p.prev_employee_id)].append(p)
    lines: dict[OpsBoundary, list[str]] = defaultdict(list)
    for (boundary, _), group in by_prev.items():
        names = sorted({p.next_name or p.next_employee_id or "" for p in group})
        if len(names) > 1:
            listed = " & ".join(names[:4])
            if len(names) > 4:
                listed += f" (+{len(names) - 4} more)"
            lines[boundary].append(f"{group[0].prev_name} -> {listed}")
    out: list[WarningRow] = []
    for boundary, items in sorted(lines.items(), key=lambda kv: kv[0].value):
        head = (
            f"{boundary.value}: the next shift has more people than the "
            f"previous one, so {len(items)} planner(s) plan the first "
            f"flights of more than one person: "
        )
        # Warning text is capped at 500 characters: list as many as fit.
        shown: list[str] = []
        for item in sorted(items):
            more = len(items) - len(shown) - 1
            tail = f"; and {more} more (see Pair_Map)" if more else ""
            if len(head) + len("; ".join([*shown, item])) + len(tail) > 480:
                break
            shown.append(item)
        rest = len(items) - len(shown)
        message = head + "; ".join(shown)
        if rest:
            message += f"{'; ' if shown else ''}and {rest} more (see Pair_Map)"
        out.append(WarningRow(
            severity=Severity.INFO, code="W216", date=d_day, message=message,
        ))
    return out


def m1_help_notes(pairs: list[Pair], d_day: date_t) -> list[WarningRow]:
    """One INFO line when M1 people plan some of A's first flights
    because M could not cover them all (2026-10-16)."""
    help_ = [
        p for p in pairs
        if p.boundary == OpsBoundary.M1_TO_A and p.preplan_count > 0
        and p.prev_employee_id and p.next_employee_id
    ]
    if not help_:
        return []
    flights = sum(p.preplan_count for p in help_)
    helpers = {p.prev_employee_id for p in help_}
    head = (
        f"M1->A: M could not plan every A flight that departs before its "
        f"holder's shift starts, so {len(helpers)} M1 person(s) plan "
        f"{flights} of them: "
    )
    items = sorted(
        f"{p.prev_name} -> {p.next_name}"
        + (f" ({p.preplan_count})" if p.preplan_count > 1 else "")
        for p in help_
    )
    shown: list[str] = []
    for item in items:
        if len(head) + len("; ".join([*shown, item])) + 40 > 480:
            break
        shown.append(item)
    message = head + "; ".join(shown)
    if len(shown) < len(items):
        message += f"; and {len(items) - len(shown)} more (see Pair_Map)"
    return [WarningRow(
        severity=Severity.INFO, code="W217", date=d_day, message=message,
    )]


__all__ = ["m1_help_notes", "shared_planner_notes", "validate_pair_discrepancies"]
