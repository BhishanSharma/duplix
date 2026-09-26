"""The Setup sidebar's shift break length.

Stored as ``break_pass.length_minutes`` in ``configs/config.yml`` and
read by the floating-break post-pass (``allocator/postpass_break.py``)
on every Allocate run. The write is a targeted line splice so the
file's commentary survives; a config written before the setting
existed gets the section appended.
"""

from __future__ import annotations

import re

from ...config import (
    BREAK_MINUTES_HIGHEST,
    BREAK_MINUTES_LOWEST,
    load_config,
)
from .config_yaml import CONFIG_YML, config_write_lock

_SECTION_RE = re.compile(r"^break_pass\s*:\s*(#.*)?$")
_LENGTH_RE = re.compile(r"^(\s+length_minutes\s*:\s*)\d+(\s*(?:#.*)?)$")


def get_break_length() -> int:
    """The break length the next Allocate run will use."""
    return load_config(CONFIG_YML).break_pass.length_minutes


def validate_break_length(value: object) -> int:
    """Whole minutes within the allowed band, or ``ValueError`` with a
    user-facing message."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("break length must be a whole number of minutes")
    try:
        minutes = int(str(value).strip())
    except ValueError:
        raise ValueError(
            "break length must be a whole number of minutes"
        ) from None
    if not BREAK_MINUTES_LOWEST <= minutes <= BREAK_MINUTES_HIGHEST:
        raise ValueError(
            f"break length must be between {BREAK_MINUTES_LOWEST} and "
            f"{BREAK_MINUTES_HIGHEST} minutes (got {minutes})"
        )
    return minutes


def set_break_length(value: object) -> int:
    """Validate ``value`` and write it to config.yml. Returns the saved
    minutes."""
    minutes = validate_break_length(value)
    with config_write_lock:
        lines = CONFIG_YML.read_text(encoding="utf-8").splitlines(keepends=False)
        header_idx = next(
            (i for i, ln in enumerate(lines) if _SECTION_RE.match(ln)), -1,
        )
        if header_idx < 0:
            lines += [
                "",
                "# Floating mid-shift break (allocator/postpass_break.py).",
                "# Editable from the Setup sidebar.",
                "break_pass:",
                f"  length_minutes: {minutes}",
            ]
        else:
            length_idx = -1
            for i in range(header_idx + 1, len(lines)):
                ln = lines[i]
                if ln.strip() and not ln[0].isspace():
                    break  # next top-level key: left the section
                if _LENGTH_RE.match(ln):
                    length_idx = i
                    break
            if length_idx >= 0:
                lines[length_idx] = _LENGTH_RE.sub(
                    lambda m: f"{m.group(1)}{minutes}{m.group(2)}",
                    lines[length_idx],
                )
            else:
                lines.insert(header_idx + 1, f"  length_minutes: {minutes}")
        CONFIG_YML.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return minutes
