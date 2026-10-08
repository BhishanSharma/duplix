"""Auto-fill a P2F handler from the roster's P2F licence column.

A shift that has P2F flights needs a nominated handler. Normally that
comes from a roster cell (``M/P2F``) or a ``type=p2f`` override. When
neither exists, this picks one — but ONLY someone the roster itself
confirms is valid:

* rostered on that exact shift today (M / A / N), i.e. live and assignable;
* not someone the operator excluded from P2F for today;
* a plain STAFF member (ZCs run the shift, so they are never auto-picked);
* the roster's licence cell genuinely contains ``P2F`` — a licence that
  was auto-granted for the day (from a handler cell or an override) does
  not count, and neither does ``Corendon``-only.

A shift needs one handler per 8 P2F flights (H17). Existing nominations are
kept and only the shortfall is filled. If too few people qualify the gap is
left for the operator to fill by hand; nothing is guessed. The picks are
deterministic: qualifying people in roster order.

Pure functions, no solver / state dependency, so the dashboard readback
and the allocator share the exact same rule.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import NamedTuple


class Candidate(NamedTuple):
    employee_id: str
    name: str
    role: str            # "STAFF" / "ZC" / "AM"
    shift: str | None    # shift rostered today, None if off
    license: str | None  # roster licence text


def has_confirmed_p2f_licence(license_text: str | None) -> bool:
    """True only for a licence the roster itself states (``P2F``,
    ``P2F/Corendon``); False for blank, ``Corendon``-only, or the
    ``(auto ...)`` tags the allocator stamps on for a single day."""
    if not license_text:
        return False
    up = license_text.upper()
    return "P2F" in up and "AUTO" not in up


def is_valid_autofill(c: Candidate, shift: str) -> bool:
    return (
        c.role == "STAFF"
        and c.shift == shift
        and has_confirmed_p2f_licence(c.license)
    )


def handlers_needed(p2f_flight_count: int, per_handler_cap: int = 8) -> int:
    """One handler per ``per_handler_cap`` P2F flights (H17), rounded up."""
    return -(-p2f_flight_count // per_handler_cap) if p2f_flight_count else 0


def autofill_handlers(
    candidates: Iterable[Candidate],
    needed_by_shift: Mapping[str, int],
    already: Mapping[str, Collection[str]] | None = None,
    excluded_ids: Collection[str] = (),
) -> dict[str, list[Candidate]]:
    """Top each shift up to ``needed_by_shift[shift]`` handlers.

    ``already`` holds the employee_ids already nominated per shift (roster
    cells / manual nominations); only the shortfall is filled, so manual
    choices are never replaced. Returns ``{shift: [new candidates]}`` for
    shifts that got at least one; a shift with too few qualifying people
    gets as many as exist (the rest stay a gap the operator must fill).
    ``excluded_ids`` are people the operator took off P2F for today.
    """
    already = already or {}
    skip = set(excluded_ids)
    pool = [c for c in candidates if c.employee_id not in skip]
    out: dict[str, list[Candidate]] = {}
    for shift, needed in needed_by_shift.items():
        have = set(already.get(shift, ()))
        short = needed - len(have)
        if short <= 0:
            continue
        for c in pool:
            if short <= 0:
                break
            if c.employee_id in have or not is_valid_autofill(c, shift):
                continue
            out.setdefault(shift, []).append(c)
            have.add(c.employee_id)
            short -= 1
    return out
