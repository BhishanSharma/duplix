"""Editable shift hours — validation and the saved file.

The Setup sidebar's "Shift timings" section lets the operator change each
shift's nominal start and end. The choice lives in
``configs/shift_times.json`` (absent = the shipped hours in
``windows.py``) and is applied by ``windows.apply_shift_hours``.

This module is deliberately free of ``windows`` imports: ``windows``
loads the saved file when it is first imported, so the dependency has to
run one way only.

File layout (24-hour clock, N's end is on the next day)::

    {"M": {"start": "04:00", "end": "12:30"}, ..., "N": {...}}
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

SHIFTS = ("M", "M1", "A", "A1", "N")

#: Only N may run past midnight; its end is read as the next morning.
OVERNIGHT_SHIFT = "N"

#: Flights sit on a 5-minute grid, and the H15 awkward-routing table is
#: keyed by exact minute, so shift times move in 5-minute steps too.
STEP_MIN = 5

#: A shift shorter or longer than this is almost certainly a typo
#: (08:30 for 18:30, say), not a real roster pattern.
MIN_LENGTH_MIN = 4 * 60
MAX_LENGTH_MIN = 12 * 60

#: Neighbouring shifts must end at least this far apart — the same as the
#: handover window, so one shift's tail extension never swallows the next
#: shift's end.
MIN_END_GAP_MIN = 30

_PATH = Path(__file__).resolve().parents[2] / "configs" / "shift_times.json"
_lock = threading.RLock()

_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

Hours = dict[str, tuple[int, int]]


def parse_clock(value: object, where: str) -> int:
    """``"HH:MM"`` → minutes after midnight, or ``ValueError``."""
    match = _CLOCK_RE.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ValueError(f"{where}: use 24-hour HH:MM, like 04:00 (got {value!r})")
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    if minutes % STEP_MIN:
        raise ValueError(f"{where}: times move in {STEP_MIN}-minute steps (got {value})")
    return minutes


def format_clock(minutes: int) -> str:
    """Ops-day minutes → ``"HH:MM"`` (past 24:00 wraps to the next day)."""
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def to_hours(raw: object) -> Hours:
    """Validate ``{shift: {"start", "end"}}`` and return it as ops-day
    minutes ``{shift: (start, end)}``. Raises ``ValueError`` with a
    message the operator can act on."""
    if not isinstance(raw, dict):
        raise ValueError("shift times must be an object of {shift: {start, end}}")
    unknown = sorted(set(raw) - set(SHIFTS))
    missing = [s for s in SHIFTS if s not in raw]
    if unknown:
        raise ValueError(f"unknown shift(s): {', '.join(unknown)}")
    if missing:
        raise ValueError(f"missing shift(s): {', '.join(missing)}")

    hours: Hours = {}
    for shift in SHIFTS:
        entry = raw[shift]
        if not isinstance(entry, dict):
            raise ValueError(f"{shift}: expected {{start, end}}")
        start = parse_clock(entry.get("start"), f"{shift} start")
        end = parse_clock(entry.get("end"), f"{shift} end")
        if end <= start:
            if shift != OVERNIGHT_SHIFT:
                raise ValueError(
                    f"{shift}: end must be after start — only {OVERNIGHT_SHIFT} "
                    "runs past midnight"
                )
            end += 1440
        if not MIN_LENGTH_MIN <= end - start <= MAX_LENGTH_MIN:
            raise ValueError(
                f"{shift}: a shift must be {MIN_LENGTH_MIN // 60}–"
                f"{MAX_LENGTH_MIN // 60} hours long "
                f"(got {(end - start) // 60} h {(end - start) % 60} min)"
            )
        hours[shift] = (start, end)

    # The shifts form a ladder (M, M1, A, A1, N): each one starts and ends
    # later than the one before. Relief, pairing and the handover windows
    # all assume it.
    for prev, nxt in zip(SHIFTS, SHIFTS[1:]):
        if hours[nxt][0] <= hours[prev][0]:
            raise ValueError(f"{nxt} must start after {prev}")
        if hours[nxt][1] - hours[prev][1] < MIN_END_GAP_MIN:
            raise ValueError(
                f"{nxt} must end at least {MIN_END_GAP_MIN} minutes after {prev}"
            )
    return hours


def to_clock(hours: Hours) -> dict[str, dict[str, str]]:
    """Inverse of :func:`to_hours`, for the API and the saved file."""
    return {
        shift: {"start": format_clock(hours[shift][0]),
                "end": format_clock(hours[shift][1])}
        for shift in SHIFTS
    }


def load_saved() -> Hours | None:
    """The saved hours, or ``None`` when nothing has been saved. A file
    that no longer validates raises rather than quietly planning with
    the wrong hours."""
    with _lock:
        if not _PATH.exists():
            return None
        try:
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            return to_hours(raw)
        except (ValueError, OSError) as exc:
            raise ValueError(f"{_PATH.name} is not usable: {exc}") from exc


def save(raw: object) -> Hours:
    """Validate and write the hours. Returns them as ops-day minutes."""
    hours = to_hours(raw)
    with _lock:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(to_clock(hours), indent=2) + "\n", encoding="utf-8",
        )
        os.replace(tmp, _PATH)  # a half-written file never replaces a good one
    return hours


def clear() -> None:
    """Forget the saved hours — the shipped ones apply again."""
    with _lock:
        _PATH.unlink(missing_ok=True)
