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

    out: list[WarningRow] = []
    for (boundary, side, emp_id, emp_name), group in by_role.items():
        if len(group) <= 1:
            continue
        # Sanctioned A→N split-1+2 exception.
        if boundary == OpsBoundary.A_TO_N and _is_a_to_n_split(group):
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


__all__ = ["validate_pair_discrepancies"]
