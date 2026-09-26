"""Independent re-check of the hard rules on a finished allocation.

The CP-SAT model and the two post-passes (P2F, INTL) each enforce parts of
H1 / H10 / H16, and the post-passes move flights *after* the solver has
finished. This module re-derives the rules from the final rows alone, so a
regression in any stage shows up as a concrete list of violations instead
of a quietly worse roster.

It is deliberately decoupled from the solver: it only needs rows that carry
``date``, ``std``, ``flt``, ``dep``, ``arr`` and ``staff_employee_id``
(``AllocationRow`` qualifies, so does a ``SimpleNamespace`` in a test),
plus ``is_international`` and ``sheet_target`` when a row has them.

Rules checked
-------------
H1   a flight appears against at most one staff member
H10  same-staff spacing (15 min, or 30 min for a domestic flight followed
     by an international one and for two P2F flights — see
     ``windows.required_spacing_min``)
H16  per-staff hard cap (only when ``caps`` is supplied)

Rows with an empty ``staff_employee_id`` (pre-plan-only / unallocated) are
ignored: they are reported elsewhere and have no owner to check.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date as date_t

from .windows import (
    SPACING_MAX_MIN,
    SpacingKey,
    required_spacing_min,
    std_to_ops_day_minutes,
)


def _spacing_key(row, ops_day: date_t) -> SpacingKey:
    """A row's H10 identity. Rows carry no ops class, so P2F is read
    off the lane they were routed to; missing fields read as a plain
    domestic flight."""
    return (
        std_to_ops_day_minutes(row.std, row.date, ops_day),
        bool(getattr(row, "is_international", False)),
        str(getattr(row, "sheet_target", "")) == "P2F",
    )


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
        # neighbours: a domestic flight 20 min before an INTL one breaks
        # the 30-min rule even with a flight in between) ----
        timed = sorted((_spacing_key(r, ops_day), flight_key(r)) for r in flights)
        for i, (a, a_key) in enumerate(timed):
            for b, b_key in timed[i + 1:]:
                gap = b[0] - a[0]
                if gap >= SPACING_MAX_MIN:
                    break
                if gap < required_spacing_min(a, b):
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
