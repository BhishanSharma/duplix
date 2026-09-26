"""Load and validate ``configs/config.yml`` (plan §1.5.1)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

#: Floating mid-shift break length, in minutes. The bounds are shared by
#: the config model below and the Setup sidebar's edit form: under 15 is
#: shorter than the H10 flight spacing, and every shift is 8.5 h with the
#: first and last hour kept break-free.
BREAK_MINUTES_DEFAULT = 30
BREAK_MINUTES_LOWEST = 15
BREAK_MINUTES_HIGHEST = 120


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AllocationConfig(_Frozen):
    cycle_year: int
    default_date: date


class SolverConfig(_Frozen):
    max_seconds: int
    deficit_penalty_lambda: int
    # CPU cores handed to CP-SAT for parallel portfolio search. 0 (default)
    # means "auto": use every available core, capped at 8 (see
    # solver/allocator_cpsat.py — OR-Tools' own guidance is that portfolio
    # search gets most of its benefit by ~8 diverse workers). Set explicitly
    # only if you know this machine should use more/fewer.
    num_workers: int = 0


class StateConfig(_Frozen):
    staleness_max_days: int


class SVPortalConfig(_Frozen):
    sheet: str
    columns: dict[str, str]
    date_format: str
    pax_regex: str
    # Phase 3 / Change 1 (2026-05-14, INTL overhaul): flights with
    # ``DEP ∈ routing_drop_dep_codes`` are dropped at Step 1 extraction.
    # ARR-side highlighting was removed in the same amendment — Gulf-3
    # ARR flights are plain domestic now.
    routing_drop_dep_codes: list[str] = []
    # Step 1 tags a flight is_international=True if **DEP** is in this
    # list (Change 9, 2026-05-14: DEP-side only — was DEP OR ARR).
    international_airport_codes: list[str] = []


class StaffRosterConfig(_Frozen):
    sheet: str
    name_column: str
    date_header_format: str
    skip_blank_columns: bool


class AMRosterConfig(_Frozen):
    sheet_glob: str
    name_column: str
    date_header_format: str
    subheader_rows_to_skip: int


class IOConfig(_Frozen):
    sv_portal: SVPortalConfig
    staff_roster: StaffRosterConfig
    am_roster: AMRosterConfig


class StatusConfig(_Frozen):
    aliases: dict[str, str]
    shifts: list[str]
    non_shift_recognized: list[str]
    am_shifts: list[str]
    zc_shifts: list[str]
    unrecognized_treatment: str


class P2FAdjustmentConfig(_Frozen):
    """User direction 2026-05-12 §3 — P2F post-pass tolerance window.

    For each P2F flight assigned to a handler, the post-pass removes:
      * 2 flights with STD within ±tolerance_minutes of (P2F_STD - 3h)
      * 1 flight with STD within ±tolerance_minutes of (P2F_STD - 1h)
      * 1 flight with STD within ±tolerance_minutes of (P2F_STD + 20min)
    Removed flights go to the §4 redistribution pool. Partial removal
    allowed (e.g., only 1 of 2 candidates found within tolerance).

    2026-05-28 (user direction): ``logic_v2`` switches the P2F post-pass
    from the anchored 3-point model (D-3h / D-1h / D+20m, host-vs-owner
    split) to a single union window per P2F flight. For each P2F at
    STD=T, the entire P2F handler chain (owner shift's handler +
    pre-planner shift's handler) loses ALL their flights in [T-2h, T+1h]
    — the P2F flight itself stays with the owner. Removed flights enter
    the redistribution pool. When ``logic_v2: true`` the P2F post-pass
    also runs BEFORE the INTL post-pass (default order is INTL first)
    so INTL D-75 removal doesn't waste a slot the P2F window would
    already free.
    """

    tolerance_minutes: int = 15
    logic_v2: bool = False


class BreakPassConfig(_Frozen):
    """Floating mid-shift break (``allocator/postpass_break.py``). Every
    on-shift staff member gets one flight-free window of
    ``length_minutes``. Editable from the Setup sidebar."""

    length_minutes: int = Field(
        default=BREAK_MINUTES_DEFAULT,
        ge=BREAK_MINUTES_LOWEST,
        le=BREAK_MINUTES_HIGHEST,
    )


class Config(_Frozen):
    allocation: AllocationConfig
    # Task 2a (2026-05-12): required_staffing removed. The per-shift
    # "people needed" baseline is now derived at display time from the
    # current preferred targets and the day's flight count
    # (ceil(flights / preferred_per_staff)). Editable values for
    # per-(shift, role) preferred / acceptable / cap live in
    # configs/shift_limits.json — see allocator/caps.py.
    solver: SolverConfig
    state: StateConfig
    io: IOConfig
    status: StatusConfig
    ops_class_by_aircraft_type: dict[str, str] = {}
    # GULF classification (user direction 2026-05-19): a flight is GULF
    # when EITHER its DEP airport (3-letter) is in
    # ``ops_class_gulf_dep_codes`` (default AUH / DOH / DXB), OR its
    # Aircraft Owner / TYPE letter equals one of
    # ``ops_class_gulf_owner_codes`` (default QR). GULF lands on the
    # dedicated GULF class for reference but is NOT read
    # by Step 3 — extract-only, never allocated. Replaces the legacy
    # ``routing_drop_dep_codes`` outright drop.
    ops_class_gulf_dep_codes: list[str] = []
    ops_class_gulf_owner_codes: list[str] = []
    # User-defined extraction filters (per user direction 2026-05-25).
    # Each filter is a dict with any subset of keys: ``ac_type``,
    # ``ac_owner``, ``ac``, ``dep``, ``arr``, plus an optional
    # ``custom_header`` + ``custom_value`` for matching arbitrary
    # SV-portal columns. Empty / missing field means "don't match on
    # that field". A flight matches the filter iff ALL specified fields
    # match (AND semantics, case-insensitive exact match). When a
    # flight matches any filter it routes to the GULF class — the
    # existing extract-only sheet — same as the static Gulf codes
    # above. Edits via the Override drawer's Extraction Filters form
    # or directly in config.yml.
    extraction_filters: list[dict[str, str]] = []
    p2f_adjustment: P2FAdjustmentConfig = P2FAdjustmentConfig()
    break_pass: BreakPassConfig = BreakPassConfig()


def load_config(path: Path | str) -> Config:
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Config.model_validate(raw)
