"""Shared primitives for the readback modules.

Everything here is pure: it reads ``AppState`` (or the config file) and
returns plain values. Keeping it in one place means a new readback
module never has to re-derive the run date or the INTL code set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...schemas import CrewStatus
from ...state import AppState

# Absolute path to configs/config.yml relative to THIS source file, so a
# readback works whatever CWD the server was launched from.
CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "config.yml"

#: Canonical shift order used by every per-shift readback.
SHIFTS: tuple[str, ...] = ("M", "A", "N", "M1", "A1")


def intl_codes() -> set[str]:
    from ...config import load_config
    cfg = load_config(CONFIG_PATH)
    return {c.upper() for c in cfg.io.sv_portal.international_airport_codes}


def run_date_iso(state: AppState) -> str:
    return state.run_date.isoformat() if state.run_date else ""


def is_live(state: AppState, av: Any) -> bool:
    """A roster row counts for the dashboard when it is assignable, sits
    on a shift, and belongs to the run date."""
    if not av.assignable or not av.current_shift:
        return False
    d_iso = run_date_iso(state)
    return not d_iso or av.date.isoformat() == d_iso


def canonical_status(raw: str) -> CrewStatus | None:
    """Resolve a status literal, tolerating the legacy slash-reversed
    form (``P2F/M``) by sorting the parts alphabetically."""
    try:
        return CrewStatus(raw)
    except ValueError:
        pass
    if "/" in raw:
        canonical = "/".join(
            sorted(p.strip().upper() for p in raw.split("/") if p.strip())
        )
        try:
            return CrewStatus(canonical)
        except ValueError:
            return None
    return None


__all__ = [
    "CONFIG_PATH",
    "SHIFTS",
    "canonical_status",
    "intl_codes",
    "is_live",
    "run_date_iso",
]
