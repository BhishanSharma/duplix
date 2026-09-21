"""Independent re-check of the hard rules on a finished allocation.

The CP-SAT model and the two post-passes (P2F, INTL) each enforce parts of
H1 / H10 / H16, and the post-passes move flights *after* the solver has
finished. This module re-derives the rules from the final rows alone, so a
regression in any stage shows up as a concrete list of violations instead
of a quietly worse roster.

It is deliberately decoupled from the solver: it only needs rows that carry
``date``, ``std``, ``flt``, ``dep``, ``arr`` and ``staff_employee_id``
(``AllocationRow`` qualifies, so does a ``SimpleNamespace`` in a test).

Rules checked
-------------
H1   a flight appears against at most one staff member
H10  same-staff spacing (15 min, or 10 min when both flights sit in a
     relaxed band — see ``windows.required_spacing_min``)
H16  per-staff hard cap (only when ``caps`` is supplied)

Rows with an empty ``staff_employee_id`` (pre-plan-only / unallocated) are
ignored: they are reported elsewhere and have no owner to check.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date as date_t

from .windows import required_spacing_min, std_to_ops_day_minutes

# Largest gap that can ever be a spacing violation (SPACING_HARD_MIN).
_MAX_REQUIRED_GAP_MIN = 15


@dataclass(frozen=True)
class Violation:
    """One broken rule. ``employee_id`` is empty for flight-level rules."""

    rule: str
    employee_id: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        who = f" [{self.employee_id}]" if self.employee_id else ""
        return f"{self.rule}{who}: {self.detail}"


def flight_key(row) -> str:
    """Stable identity for a flight row (mirrors ``FlightInput.unique_id``)."""
    return f"{row.flt}|{row.dep}|{row.arr}|{row.std.isoformat()}|{row.date.isoformat()}"


def check_allocation(
    rows: Iterable,
    *,
    ops_day: date_t,
    caps: Mapping[str, int] | None = None,
    waived_pairs: Collection[frozenset[str]] = (),
) -> list[Violation]:
    """Return every H1 / H10 / H16 violation found in ``rows``.

    ``caps`` maps employee_id -> hard cap (H16). Staff missing from the
    mapping are not cap-checked.
    ``waived_pairs`` holds ``frozenset({key_a, key_b})`` entries for pairs
    an operator explicitly waived (the ``waive_h10_pair`` override); those
    pairs are not reported for H10.
    """
    assigned = [r for r in rows if getattr(r, "staff_employee_id", "")]
    violations: list[Violation] = []

    # ---- H1: no flight owned twice ----
    seen = Counter(flight_key(r) for r in assigned)
    for key, n in sorted(seen.items()):
        if n > 1:
            violations.append(Violation("H1", "", f"{key} assigned {n} times"))

    by_staff: dict[str, list] = defaultdict(list)
    for r in assigned:
        by_staff[r.staff_employee_id].append(r)

    for emp_id, flights in sorted(by_staff.items()):
        # ---- H16: hard cap ----
        if caps is not None and emp_id in caps and len(flights) > caps[emp_id]:
            violations.append(
                Violation("H16", emp_id, f"{len(flights)} flights > cap {caps[emp_id]}")
            )

        # ---- H10: spacing (all pairs inside the max window, not just
        # neighbours: a relaxed 10-min pair can sit next to a 4-min one) ----
        timed = sorted(
            ((std_to_ops_day_minutes(r.std, r.date, ops_day), flight_key(r)) for r in flights)
        )
        for i, (a_min, a_key) in enumerate(timed):
            for b_min, b_key in timed[i + 1:]:
                gap = b_min - a_min
                if gap >= _MAX_REQUIRED_GAP_MIN:
                    break
                if gap < required_spacing_min(a_min, b_min):
                    if frozenset({a_key, b_key}) in waived_pairs:
                        continue
                    violations.append(
                        Violation(
                            "H10",
                            emp_id,
                            f"{a_key} and {b_key} only {gap} min apart",
                        )
                    )
    return violations
