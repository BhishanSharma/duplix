"""Pydantic schemas for the flight-allocation pipeline (plan §4)."""

from __future__ import annotations

from datetime import date as date_t
from datetime import datetime as datetime_t
from datetime import time as time_t
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CrewStatus(StrEnum):
    """Plan §4.1 — every legal value a roster cell may carry."""

    UNDERSTUDY = "U"
    DAY_OFF = "F"
    PAID_LEAVE = "P/L"
    CASUAL_LEAVE = "C/L"
    CUSTOM_OFF = "C/OFF"
    TRAINING = "TR"
    SHIFT_M = "M"
    SHIFT_A = "A"
    SHIFT_N = "N"
    SHIFT_M1 = "M1"
    SHIFT_A1 = "A1"
    AM_M_IGT = "M/IGT"
    AM_A_IGT = "A/IGT"
    ZC_M = "M/ZC"
    ZC_A = "A/ZC"
    ZC_N = "N/ZC"
    ZC_M1 = "M1/ZC"
    ZC_A1 = "A1/ZC"
    # Per user direction 2026-05-10: roster can directly mark a staff
    # member as the P2F handler for a shift. Cell value `M/P2F` means
    # "on M shift today AND the P2F handler for M". Engine auto-picks
    # without needing an override row.
    # 2026-05-21 (user direction): canonical form is shift-first then
    # role-suffix — M/P2F, A/P2F, N/P2F — matching the M/ZC, A/ZC,
    # N/ZC convention. Legacy P2F/M / P2F/A / P2F/N still accepted via
    # the canonicalization path in readers.normalize_status (parts
    # sorted alphabetically — M < P2F, A < P2F, N < P2F).
    P2F_M = "M/P2F"
    P2F_A = "A/P2F"
    P2F_N = "N/P2F"
    # 2026-05-26 (user direction): a single roster cell can carry BOTH
    # roles. `M/ZC/P2F` (or `M/P2F/ZC`, any order) marks the staff as
    # the M-shift ZC AND the M-shift P2F handler. Canonical form is
    # alphabetical: M < P2F < ZC. Cap stays at ZC's lower band (14-15
    # day / 10-11 night), applied to regular+P2F combined; the D-1hr
    # and D+20min ±15 partial windows are skipped for these handlers
    # so the buffer attrition doesn't compound with the ZC cap.
    ZC_P2F_M = "M/P2F/ZC"
    ZC_P2F_A = "A/P2F/ZC"
    ZC_P2F_N = "N/P2F/ZC"
    ZC_P2F_M1 = "M1/P2F/ZC"
    ZC_P2F_A1 = "A1/P2F/ZC"
    OTHER = "OTHER"
    BLANK = ""


NON_ASSIGNABLE: frozenset[CrewStatus] = frozenset({
    CrewStatus.UNDERSTUDY,
    CrewStatus.TRAINING,
    CrewStatus.PAID_LEAVE,
    CrewStatus.CASUAL_LEAVE,
    CrewStatus.CUSTOM_OFF,
    CrewStatus.OTHER,
})


class OpsClass(StrEnum):
    """Sheet routing for the cleaned flights output (plan §4.13).

    Per user direction 2026-05-11:
      - TEST: TYPE=X AND dep==arr (same-airport loop)
      - FERRY: TYPE=X AND dep!=arr (positioning leg) — also TYPE in K/P/T/G
        (legacy ferry mapping)
      - CHARTER: TYPE=B (charter passenger flight, new class)
      - All three are allocated as normal flights and counted under
        workload (no special per-staff cap). The cleaned output groups
        them into a single SpecialOps group with one table
        per class so the assigner sees them side by side.

    Per user direction 2026-05-19:
      - GULF: flights touching AUH / DOH / DXB (DEP-side) OR with
        Aircraft Owner=QR. EXTRACT-ONLY — visible on the dedicated
        GULF class for reference, but Step 3 does NOT read
        that sheet so these flights are NEVER allocated to staff. Wins
        over the legacy ``routing_drop_dep_codes`` outright drop.
    """

    DAY = "day"
    NIGHT = "night"
    P2F = "p2f"
    FERRY = "ferry"
    NORSE = "norse"
    TEST = "test"
    CHARTER = "charter"
    GULF = "gulf"


class RawFlightRow(BaseModel):
    """Raw SV portal row — permissive (plan §4.3 / §1.5.1).

    Strict pax parsing is moved to Step 1's drop filter. Unknown columns are
    ignored.
    """

    model_config = ConfigDict(extra="ignore")

    flight_id: str | None = None
    departure: str | None = None
    arrival: str | None = None
    dep_time: time_t | None = None
    arr_time: time_t | None = None
    aircraft_type: str | None = None
    aircraft_subtype: str | None = None
    # Aircraft Owner carrier code. Used to classify NORSE per user
    # direction 2026-05-11: owner code `N0` (or `AC-789`) → NORSE,
    # everything else routes by TYPE / date as before.
    owner: str | None = None
    date: date_t | None = None
    booked_pax_raw: str | None = None
    # 2026-05-25 (user direction — extraction filters): capture every
    # SV-portal cell by its raw header so the extraction-filter system
    # can match on arbitrary columns (e.g. Crew, REG, MEMO) that
    # aren't part of the canonical schema. Populated by
    # ``read_sv_portal_from_workbook``; keys are the literal header
    # strings, values are stringified cell contents (or None).
    extra_columns: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "flight_id", "aircraft_subtype", "owner", "booked_pax_raw",
        mode="before",
    )
    @classmethod
    def _coerce_to_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v)


class CleanFlightRow(BaseModel):
    """Cleaned flight row written to one of the day_ops/night_ops/... sheets.

    Phase 3 (2026-05-14, INTL overhaul):
      - ``is_international``: **DEP-side only** — ``dep ∈ intl_codes``
        (was ``dep OR arr``). Rendered yellow in workbook + browser.
      - The legacy ``routing_via_excluded_hub`` field (pink-highlight for
        Gulf-3 ARR) was removed; Gulf-3 ARR flights are plain domestic now.
        Gulf-3 DEP flights are dropped at Step 1.
      - ``is_preplan_deferred``: D+1 flights with STD ∈ [05:05, 05:30]
        enter the pool but are NOT allocated today; Step 3 re-appends
        them with warning="PREPLAN_DEFERRED".
    """

    model_config = ConfigDict(frozen=True)

    date: date_t
    flt: str
    type: str
    ac: str
    dep: str
    arr: str
    std: time_t
    load: int = Field(ge=0, le=600)
    ops_class: OpsClass
    is_international: bool = False
    is_preplan_deferred: bool = False

    @field_validator("dep", "arr")
    @classmethod
    def _three_letters(cls, v: str) -> str:
        v = v.upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError(f"expected 3 letters, got {v!r}")
        return v

    @field_validator("flt")
    @classmethod
    def _flt_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("flt must be non-empty")
        return v

    @field_validator("type")
    @classmethod
    def _type_upper(cls, v: str) -> str:
        # 2026-05-14: simplified SV portal exports drop the J/B/K/X letter
        # column; ``type`` is allowed to be empty in that case. Ops_class
        # then falls back to date-based DAY/NIGHT classification (see
        # _derive_ops_class). Older exports that DO publish the letter
        # column still benefit from the explicit per-letter mapping.
        return v.upper()

    @field_validator("ac")
    @classmethod
    def _ac_upper_lenient(cls, v: str) -> str:
        # Real-world SV exports often leave AC blank; tolerate it.
        return v.upper() if v else ""


# ---------- Phase 2: roster extraction ----------


class Role(StrEnum):
    """Plan §4.5/§4.6 — STAFF for staff_roster.xlsx; AM/ZC inferred for am_roster.xlsx."""

    STAFF = "STAFF"
    AM = "AM"
    ZC = "ZC"


ShiftCode = Literal["M", "A", "N", "M1", "A1"]
"""The five plain shift codes that populate AvailabilityRow.current_shift."""

SHIFT_CODES: frozenset[str] = frozenset({"M", "A", "N", "M1", "A1"})


# ============================================================================
# CENTRAL STATUS TAXONOMY (2026-05-27)
# ----------------------------------------------------------------------------
# Single source of truth for what each CrewStatus implies. Every reader,
# writer, dashboard panel, eligibility filter that needs to ask "what
# shift does this status mean?" / "does this status make them a P2F
# handler?" / "does this status imply ZC role?" should look up here —
# NOT carry its own copy of `if status in (...)`.
#
# WHEN ADDING A NEW STATUS:
#   1. Add the enum member to CrewStatus above.
#   2. Add entries to the maps below as appropriate.
#   3. That's it — readback, step2 emit, step3 admit, and the eligibility
#      layer all pull from these maps, so they pick it up automatically.
# ============================================================================

# Status → shift it implies. None means the status doesn't carry a
# shift (off-day, leave, training, OTHER, BLANK).
STATUS_TO_SHIFT: dict[CrewStatus, ShiftCode | None] = {
    # Off / non-assignable
    CrewStatus.UNDERSTUDY:   None,
    CrewStatus.DAY_OFF:      None,
    CrewStatus.PAID_LEAVE:   None,
    CrewStatus.CASUAL_LEAVE: None,
    CrewStatus.CUSTOM_OFF:   None,
    CrewStatus.TRAINING:     None,
    CrewStatus.OTHER:        None,
    CrewStatus.BLANK:        None,
    # Plain shift codes
    CrewStatus.SHIFT_M:  "M",
    CrewStatus.SHIFT_A:  "A",
    CrewStatus.SHIFT_N:  "N",
    CrewStatus.SHIFT_M1: "M1",
    CrewStatus.SHIFT_A1: "A1",
    # AM (on-shift but not flying — admin/training duty)
    CrewStatus.AM_M_IGT: "M",
    CrewStatus.AM_A_IGT: "A",
    # ZC variants
    CrewStatus.ZC_M:  "M",
    CrewStatus.ZC_A:  "A",
    CrewStatus.ZC_N:  "N",
    CrewStatus.ZC_M1: "M1",
    CrewStatus.ZC_A1: "A1",
    # P2F-handler variants (STAFF that nominate as P2F handler)
    CrewStatus.P2F_M: "M",
    CrewStatus.P2F_A: "A",
    CrewStatus.P2F_N: "N",
    # Combined ZC + P2F-handler variants
    CrewStatus.ZC_P2F_M:  "M",
    CrewStatus.ZC_P2F_A:  "A",
    CrewStatus.ZC_P2F_N:  "N",
    CrewStatus.ZC_P2F_M1: "M1",
    CrewStatus.ZC_P2F_A1: "A1",
}

# Statuses that explicitly imply a particular Role. Statuses NOT in
# this set inherit role from the crew row's sheet-origin (STAFF for
# IN_Staff, AM for IN_AM_Roster). Resolved via STATUS_TO_ROLE() below
# because Role isn't defined yet at module-load time when this dict
# is built — we close over the Role enum lazily inside the helper.
_STATUS_ROLE_TAGS: dict[CrewStatus, str] = {
    # ZC variants imply ZC role for the day (overrides crew-row default).
    CrewStatus.ZC_M:  "ZC",
    CrewStatus.ZC_A:  "ZC",
    CrewStatus.ZC_N:  "ZC",
    CrewStatus.ZC_M1: "ZC",
    CrewStatus.ZC_A1: "ZC",
    CrewStatus.ZC_P2F_M:  "ZC",
    CrewStatus.ZC_P2F_A:  "ZC",
    CrewStatus.ZC_P2F_N:  "ZC",
    CrewStatus.ZC_P2F_M1: "ZC",
    CrewStatus.ZC_P2F_A1: "ZC",
    # AM/IGT variants → AM
    CrewStatus.AM_M_IGT: "AM",
    CrewStatus.AM_A_IGT: "AM",
}

# Statuses that imply ZC role for the day (subset of _STATUS_ROLE_TAGS).
STATUS_IS_ZC: frozenset[CrewStatus] = frozenset(
    s for s, tag in _STATUS_ROLE_TAGS.items() if tag == "ZC"
)

# Statuses that nominate the staff as a P2F handler. Maps to the
# SHIFT they handle P2F flights for (which equals STATUS_TO_SHIFT[s]
# for these — but kept as a separate map for clarity at call sites).
STATUS_TO_P2F_HANDLER_SHIFT: dict[CrewStatus, ShiftCode] = {
    CrewStatus.P2F_M: "M",
    CrewStatus.P2F_A: "A",
    CrewStatus.P2F_N: "N",
    CrewStatus.ZC_P2F_M:  "M",
    CrewStatus.ZC_P2F_A:  "A",
    CrewStatus.ZC_P2F_N:  "N",
    CrewStatus.ZC_P2F_M1: "M1",
    CrewStatus.ZC_P2F_A1: "A1",
}

# Convenience set form for `status in STATUS_IS_P2F_HANDLER` checks.
STATUS_IS_P2F_HANDLER: frozenset[CrewStatus] = frozenset(
    STATUS_TO_P2F_HANDLER_SHIFT.keys()
)


def role_implied_by_status(status: CrewStatus) -> "Role | None":
    """Return the Role this status forces for the day, or None when
    the status doesn't override (caller falls back to the crew row's
    sheet-origin role). Lazy lookup of Role to dodge the forward
    reference — Role is defined after this section."""
    tag = _STATUS_ROLE_TAGS.get(status)
    if tag is None:
        return None
    return Role(tag)


def is_p2f_handler_status(status: CrewStatus) -> bool:
    """True iff the status nominates the staff as a P2F handler."""
    return status in STATUS_IS_P2F_HANDLER


def p2f_handler_shift(status: CrewStatus) -> ShiftCode | None:
    """Return the shift this status nominates a P2F handler for, or
    None if the status isn't a P2F-handler status."""
    return STATUS_TO_P2F_HANDLER_SHIFT.get(status)


def is_zc_status(status: CrewStatus) -> bool:
    """True iff the status implies ZC role for the day."""
    return status in STATUS_IS_ZC


def shift_of_status(status: CrewStatus) -> ShiftCode | None:
    """Return the shift this status implies, or None for off-day /
    leave / OTHER / BLANK."""
    return STATUS_TO_SHIFT.get(status)


# Convenience: statuses that auto-grant a P2F license (because they
# nominate the staff as a P2F handler). step2 uses this to populate
# the license cell so downstream eligibility (F3) treats them as
# licensed even when the License column on the input roster was blank.
P2F_LICENSE_GRANTING_STATUSES: frozenset[CrewStatus] = STATUS_IS_P2F_HANDLER

# Shifts that nominate a plain (non-ZC) P2F handler. M1 / A1 do NOT
# nominate plain P2F handlers per the 2026-05-10 directive — only
# M / A / N. The combined ZC+P2F variant CAN appear on M1 / A1 via
# the ZC_P2F_M1 / ZC_P2F_A1 statuses; those route through
# STATUS_TO_P2F_HANDLER_SHIFT regardless of this tuple.
P2F_HANDLER_ELIGIBLE_SHIFTS: tuple[ShiftCode, ...] = ("M", "A", "N")


class OverrideType(StrEnum):
    """Every legal `type` value an override row may carry.

    Adding a new override type? Three steps:
      1. Add an enum member here.
      2. Implement the reader/applier (typically in io/readers.py or
         staged_overrides.py).
      3. If the assigner UI should offer it in the drawer's Add row
         dropdown, add to OVERRIDE_TYPES_FOR_UI (below). Frontend
         pulls the list from /api/override_types at page load, so a
         new entry shows up automatically.
    """
    # Handler nominations (engine reads at solve time)
    P2F                = "p2f"
    NORSE              = "norse"
    # Per-staff constraints (read at solve time)
    SICK               = "sick"
    CHANGE_ROLE        = "change_role"
    MAX_FLIGHTS        = "max_flights"
    CUTOFF_TIME        = "cutoff_time"
    # Staged-form mutations (apply directly to data sheets at save time)
    ADD_FLIGHT         = "add_flight"
    REMOVE_FLIGHT      = "remove_flight"
    ADD_STAFF          = "add_staff"
    REMOVE_STAFF       = "remove_staff"
    # Phase-R per-flight overrides written by the recommender
    WAIVE_H10_PAIR     = "waive_h10_pair"
    RAISE_CAP          = "raise_cap"
    SKIP_P2F_BUFFER    = "skip_p2f_buffer"
    SKIP_INTL_REMOVAL  = "skip_intl_removal"


# The subset that the drawer's Add row dropdown should expose — i.e.,
# user-facing override types. Phase-R recommender types are written
# via /api/recommender/apply, not the manual drawer flow, so they're
# excluded here.
OVERRIDE_TYPES_FOR_UI: tuple[OverrideType, ...] = (
    OverrideType.P2F,
    OverrideType.NORSE,
    OverrideType.SICK,
    OverrideType.CHANGE_ROLE,
    OverrideType.MAX_FLIGHTS,
    OverrideType.CUTOFF_TIME,
)


# Subset that the staged-form helper accepts (add/remove flight/staff).
STAGED_FORM_OVERRIDE_TYPES: tuple[OverrideType, ...] = (
    OverrideType.ADD_FLIGHT,
    OverrideType.REMOVE_FLIGHT,
    OverrideType.ADD_STAFF,
    OverrideType.REMOVE_STAFF,
)


# 2026-05-27 (user direction — "no patchwork, centralize"): the drawer
# was rendering every override column on every row regardless of
# type, so a p2f row showed empty std / other_std / dep / arr cells
# (added by Phase-R writes) crowding out the cells that actually
# matter. This map declares, per override type, which override
# header columns are relevant — the frontend uses it to gray-out
# irrelevant cells (so the assigner only sees the dropdowns that
# matter for the row's type).
#
# Adding a new OverrideType? Add its column set here so the drawer
# auto-tailors. The frontend pulls this via /api/override_types.
#
# Names are LOWERCASED matches against the override column names.
# Headers not listed here for a given type are treated as irrelevant
# and rendered as a disabled "—" placeholder (the actual cell value
# is preserved on save — we only hide it visually).
OVERRIDE_TYPE_RELEVANT_COLS: dict[OverrideType, tuple[str, ...]] = {
    # ----- User-facing (handler nominations + per-staff) -----
    OverrideType.P2F:               ("type", "employee", "shift"),
    OverrideType.NORSE:             ("type", "employee"),
    OverrideType.SICK:              ("type", "employee"),
    # change_role: the "shift" column carries the new ROLE
    # (STAFF / ZC / AM), not a shift code — the frontend swaps the
    # dropdown's option set when type=change_role.
    OverrideType.CHANGE_ROLE:       ("type", "employee", "shift"),
    OverrideType.MAX_FLIGHTS:       ("type", "employee", "limit"),
    # cutoff_time: windowed (from_time + to_time). Either may be blank;
    # the engine falls back to the staff's normal shift boundary.
    OverrideType.CUTOFF_TIME:       ("type", "employee", "from_time", "to_time"),
    # ----- Staged forms (add/remove flight/staff) -----
    OverrideType.ADD_FLIGHT: (
        "type", "flt", "dep", "arr", "std",
        "ops_class", "ac_type", "pax", "owner", "date",
    ),
    OverrideType.REMOVE_FLIGHT:     ("type", "flt", "dep", "arr", "std", "date"),
    OverrideType.ADD_STAFF:         ("type", "employee", "shift", "role"),
    OverrideType.REMOVE_STAFF:      ("type", "employee"),
    # ----- Phase-R recommender types -----
    OverrideType.WAIVE_H10_PAIR:    ("type", "employee", "flight", "std", "other_std"),
    OverrideType.RAISE_CAP:         ("type", "employee", "shift", "limit", "flight", "std"),
    OverrideType.SKIP_P2F_BUFFER:   ("type", "employee", "flight", "std"),
    OverrideType.SKIP_INTL_REMOVAL: ("type", "flight", "std"),
}


def _validate_override_relevant_cols_completeness() -> None:
    """Fail at import time if a new OverrideType is added but never
    registered in OVERRIDE_TYPE_RELEVANT_COLS — same defensive pattern
    as _validate_taxonomy_completeness for CrewStatus. Catches the
    "added enum, forgot the map" regression before it ships."""
    missing = [t for t in OverrideType if t not in OVERRIDE_TYPE_RELEVANT_COLS]
    if missing:
        raise RuntimeError(
            "OverrideType relevant-cols map incomplete — schemas."
            "OVERRIDE_TYPE_RELEVANT_COLS missing entries for: "
            f"{[m.value for m in missing]}. Add each new type's "
            "tuple of relevant header names so the drawer renders "
            "the right dropdowns and gray-outs the rest."
        )


_validate_override_relevant_cols_completeness()


def _validate_taxonomy_completeness() -> None:
    """Fail loudly at import time if a new CrewStatus member was added
    but never registered in STATUS_TO_SHIFT.

    This catches the most common "I added a new enum value but forgot
    to update the map" regression — the same class of bug that's
    bitten the engine multiple times in 2026-05. Better to crash the
    server at startup with a precise message than to silently
    misroute statuses in production.
    """
    missing = [s for s in CrewStatus if s not in STATUS_TO_SHIFT]
    if missing:
        raise RuntimeError(
            "CrewStatus taxonomy incomplete in schemas.STATUS_TO_SHIFT — "
            f"missing entries: {[s.value for s in missing]}. "
            "Every CrewStatus enum member must be registered in "
            "STATUS_TO_SHIFT (use None for off-day / leave / OTHER). "
            "Edit src/schemas.py to fix."
        )


_validate_taxonomy_completeness()
# ============================================================================
# END central status taxonomy
# ============================================================================


class CrewRosterRow(BaseModel):
    """Wide crew-roster row read from IN_Staff (plan §4.5).

    Normalization happens at read time: ``status_by_date`` holds CrewStatus values
    (with ``OFF→F`` aliases applied and unrecognized literals mapped to OTHER).
    The original literal is preserved in ``raw_status_by_date`` per §1.5.1.

    Step 4 additions:
      - ``employee_id``: stable join key, auto-generated as ``STAFF_NNN`` by
        the reader if the assigner leaves the IN_Staff cell blank. Disambiguates
        same-name records (e.g., the two DEEPAK KUMARs across IN_Staff and
        IN_AM_Roster).
      - ``license``: P2F-licensure tag. None = no special license. Values
        observed in real data: 'P2F', 'P2F+Corendon', 'Corendon'.
    """

    model_config = ConfigDict(frozen=True)
    employee_id: str
    name: str
    role: Role
    license: str | None = None
    status_by_date: dict[date_t, CrewStatus]
    raw_status_by_date: dict[date_t, str]


class AMRosterRow(BaseModel):
    """Wide AM/ZC-roster row read from IN_AM_Roster (plan §4.6).

    ``role`` is inferred at read time:
      - any ``M/IGT`` or ``A/IGT`` anywhere in the month  -> Role.AM
      - else any ``*/ZC``                                 -> Role.ZC
      - else                                              -> Role.STAFF (defensive fallback)

    Step 4 additions: ``employee_id`` (auto-generated as ``AM_NNN`` by the
    reader if blank) and ``license`` (per-row P2F tag — ZCs may also be P2F-
    licensed and act as the shift's P2F handler).
    """

    model_config = ConfigDict(frozen=True)
    employee_id: str
    name: str
    role: Role
    license: str | None = None
    status_by_date: dict[date_t, CrewStatus]
    raw_status_by_date: dict[date_t, str]


class AvailabilityRow(BaseModel):
    """Step 2 output, long format (plan §4.9 + §1.5.1).

    Step 4 additions: ``employee_id`` and ``license`` propagate verbatim from
    CrewRosterRow / AMRosterRow so downstream solvers can join by ID and
    filter by license without re-reading the source sheets.
    """

    model_config = ConfigDict(frozen=True)
    employee_id: str
    name: str
    role: Role
    date: date_t
    status: CrewStatus
    raw_status: str
    assignable: bool
    current_shift: ShiftCode | None = None
    license: str | None = None
    # Which roster the person comes from (STAFF for the staff roster, AM
    # for the AM roster), before any /ZC promotion. Lets a ZC be taken
    # back to what they were when the assigner removes them on the
    # dashboard. None for synthetic rows (add_staff).
    origin_role: Role | None = None


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class WarningRow(BaseModel):
    """Plan §4.12."""

    model_config = ConfigDict(frozen=True)
    severity: Severity
    code: str = Field(pattern=r"^W\d{3}$")
    name: str | None = None
    date: date_t | None = None
    message: str = Field(max_length=500)


# ---------- Phase 3: balance roster ----------


class OverrideMode(StrEnum):
    """Plan §4.7 — disposition of a single Overrides row.

    NO         row is informational, ignored by solver
    FORCE      hard constraint: solver must assign to_shift on (employee, date);
               infeasibility triggers W001 drop-and-retry
    VOLUNTEER  soft hint: employee has nominated themself for from_shift -> to_shift.
               Solver MAY take the swap "for free" (no swap-count penalty) if that
               helps balance, but never forces it. If the solver leaves the crew on
               from_shift, the volunteered swap is silently unused.
    """

    NO = "NO"
    FORCE = "FORCE"
    VOLUNTEER = "VOLUNTEER"


class OverrideRow(BaseModel):
    """Plan §4.7 — one row of the Overrides sheet."""

    model_config = ConfigDict(frozen=True)
    employee: str
    date: date_t
    mode: OverrideMode
    from_shift: ShiftCode | None = None
    to_shift: ShiftCode | None = None

    @model_validator(mode="after")
    def _check_active_modes_require_shifts(self) -> Self:
        if self.mode != OverrideMode.NO and (self.from_shift is None or self.to_shift is None):
            raise ValueError(f"mode={self.mode} requires both from_shift and to_shift")
        return self


# RequiredStaffingRow removed 2026-05-21 — the IN_Required sheet and
# its reader are gone, no callers reference this schema anymore.


class Origin(StrEnum):
    ROSTER = "ROSTER"
    OVERRIDE = "OVERRIDE"
    SOLVER_SWAP = "SOLVER_SWAP"


class BalancedRosterRow(BaseModel):
    """Plan §4.10 — Step 3 output, long format.

    Step 4 additions: ``employee_id`` (the join key Step 4 uses everywhere) and
    ``license`` (passed through from AvailabilityRow).
    """

    model_config = ConfigDict(frozen=True)
    employee_id: str
    name: str
    role: Role
    date: date_t
    assigned_shift: ShiftCode | None = None
    origin: Origin
    was_swapped: bool
    license: str | None = None


class SwapReason(StrEnum):
    RESPECT_OVERRIDE = "RESPECT_OVERRIDE"
    BALANCE_DEFICIT = "BALANCE_DEFICIT"
    BALANCE_SURPLUS = "BALANCE_SURPLUS"
    # Crew moved to OFF because keeping their current_shift would have violated the
    # 24-hr rule against either D-1 history or the other target date's natural
    # shift. Distinguishes a forced-off from a true surplus removal.
    RULE_24H_CONFLICT = "RULE_24H_CONFLICT"


class SwapLogRow(BaseModel):
    """Plan §4.11."""

    model_config = ConfigDict(frozen=True)
    name: str
    date: date_t
    from_shift: str | None = None
    to_shift: str | None = None
    reason: SwapReason
    timestamp_utc: datetime_t


# Plan §4.2 shift-start times (24-hr rule uses these as IntervalVar starts).
SHIFT_START: dict[str, time_t] = {
    "M": time_t(4, 0),
    "A": time_t(12, 30),
    "N": time_t(20, 30),
    "M1": time_t(6, 0),
    "A1": time_t(14, 30),
}


# ====================================================================
# Phase 4: flight allocation
#
# These models are consumed only by Step 4. Earlier-phase code (Steps 1-3)
# does not import any of them. Keeping them in schemas.py rather than a
# step4-specific module so the type vocabulary stays in one place.
# ====================================================================


class OpsBoundary(StrEnum):
    """A shift transition. Pairs are keyed by boundary; ZC pairs use the same
    boundary identifiers."""

    N_TO_M = "N->M"
    M_TO_M1 = "M->M1"
    M_TO_A = "M->A"
    M1_TO_A1 = "M1->A1"
    A_TO_N = "A->N"
    A_TO_A1 = "A->A1"  # handover-only routing for A surplus
    A1_TO_N = "A1->N"


class PairRole(StrEnum):
    """A pair's responsibility at its boundary.

    PRIMARY        — both pre-planning (per H6 counts) AND tail handover
                     (per H7 windows).
    HANDOVER_ONLY  — tail handover only; never carries pre-planning.
                     Used for A↔A1 routing when |A|>|N| and for the
                     secondary A↔N pairs when surplus A staff still remain
                     after the A1 handover route is exhausted.
    """

    PRIMARY = "primary"
    HANDOVER_ONLY = "handover_only"


class AllocationSheet(StrEnum):
    """Routing target for an AllocationRow — which allocation lane
    the row writes to."""

    DAY_OPS = "DayOps"
    NIGHT_OPS = "NightOps"
    P2F = "P2F"
    NORSE = "NORSE"


class FlightInput(BaseModel):
    """A single flight for Step 4's solver input. Mirrors CleanFlightRow but
    adds Step-4-specific tags computed during preprocessing."""

    model_config = ConfigDict(frozen=True)
    date: date_t
    flt: str
    type: str
    ac: str
    dep: str
    arr: str
    std: time_t
    load: int = Field(ge=0, le=600)
    ops_class: OpsClass
    is_international: bool = False
    is_preplan_deferred: bool = False
    """Phase 3 / Change 7: D+1 flights with STD ∈ [05:05, 05:30] enter
    the pool but are split out by Step 3 before the solver runs; re-
    appended to the output sheet with warning="PREPLAN_DEFERRED" and
    empty staff column. Solver never sees them."""
    is_awkward_window: bool = False  # H15 routing
    awkward_eligible_shifts: tuple[ShiftCode, ...] = ()
    """If is_awkward_window=True, the only shifts a flight at this STD may be
    assigned to. Empty tuple when not in an awkward window."""

    @property
    def unique_id(self) -> str:
        """A stable, unique identifier for THIS leg of the rotation.

        Per user direction 2026-05-12: multi-leg flights (same FLT number,
        different STD/dep/arr legs) must be treated as SEPARATE flights for
        allocation purposes. Earlier code keyed eligibility / x-vars by
        ``flt`` alone, which made later legs overwrite earlier ones and
        caused phantom assignments + H10 spacing violations. Keying by
        (flt, dep, arr, std, date) makes each leg an independent flight
        in the CP-SAT model — exactly what the operations team expects.

        2026-05-26 fix: date appended. SV portal exports two days at a
        time; the same daily flight (e.g. 64 JED-DEL 05:30) appears on
        both D and D+1 with identical FLT|DEP|ARR|STD. Without date in
        the key, the D+1 preplan-deferred filter collided with D-day and
        silently dropped 33 morning flights per run.
        """
        return f"{self.flt}|{self.dep}|{self.arr}|{self.std.isoformat(timespec='minutes')}|{self.date.isoformat()}"


class StaffMember(BaseModel):
    """A single staff record assembled by Step 4 from OUT_Balanced_Roster
    (STAFF) or IN_AM_Roster (ZC). AMs are not represented as StaffMember at
    all — they are filtered out at preprocessing time.
    """

    model_config = ConfigDict(frozen=True)
    employee_id: str
    name: str
    role: Role
    date: date_t
    shift_today: ShiftCode | None = None
    """None means the staff is OFF today (not assignable). Assignable staff
    have one of M/A/N/M1/A1."""
    license: str | None = None
    is_p2f_licensed: bool = False
    """Convenience flag derived from license string. True when license
    contains 'P2F'."""
    is_newbie: bool = False
    """Per override row; affects H12 international exclusion."""
    max_flights_cap: int | None = None
    """Per-staff override max; lower-bound clamp on the
    H16 hard cap. None means use the H16 default for the staff's shift+role."""
    std_cutoff: time_t | None = None
    """Per-staff override STD cutoff (UPPER bound);
    flights with STD past this are excluded for this staff. None means
    no upper bound — staff's normal shift end applies."""
    std_start: time_t | None = None
    """2026-05-27: per-staff override STD START (LOWER bound) from
    an override; flights with STD before this are excluded for this
    staff. None means no lower bound — staff's normal shift start
    applies. Together with std_cutoff, this carves a custom working
    window inside the staff's normal shift. Example: an A-staff with
    std_start=14:00 + std_cutoff=17:00 only takes flights in that
    3-hour band; one with just std_cutoff=17:00 comes in at the
    regular A start (12:30) but leaves early at 17:00."""

    @model_validator(mode="after")
    def _coherent_p2f_flag(self) -> Self:
        if self.is_p2f_licensed and not (self.license and "P2F" in self.license.upper()):
            raise ValueError(
                "is_p2f_licensed=True requires 'P2F' in license; "
                f"got license={self.license!r}"
            )
        return self


class Pair(BaseModel):
    """One pair at one boundary. The 'prev' side is the earlier shift (e.g.,
    M for the M→A boundary); 'next' is the later shift (A). Either side may
    be None for self-planning surplus, but not both — that wouldn't be a pair.

    ``preplan_count`` carries the per-pair count of flights this pair's
    prev-side plans for this pair's next-side. Default H6 values:
        N→M=1, M→M1=1, M→A=2, M1→A1=2, A→N=3, A1→N=0, A→A1=0.
    When N has 2 A-partners (split-1+2), the primary pair's count drops
    to 2 and the secondary pair carries 1. Pairs with preplan_count=0
    are handover-only.
    """

    model_config = ConfigDict(frozen=True)
    boundary: OpsBoundary
    pair_role: PairRole
    preplan_count: int = Field(default=0, ge=0, le=5)
    prev_employee_id: str | None
    prev_name: str | None
    prev_shift: ShiftCode | None
    next_employee_id: str | None
    next_name: str | None
    next_shift: ShiftCode | None

    @model_validator(mode="after")
    def _at_least_one_side_present(self) -> Self:
        if self.prev_employee_id is None and self.next_employee_id is None:
            raise ValueError("a pair must have at least one side populated")
        return self


class P2FNomination(BaseModel):
    """Per (date, shift) P2F handler nomination from an override row."""

    model_config = ConfigDict(frozen=True)
    date: date_t
    shift: ShiftCode
    employee_id: str


class NORSEHandler(BaseModel):
    """Per-day NORSE handler nomination from an override row."""

    model_config = ConfigDict(frozen=True)
    date: date_t
    employee_id: str


class PerStaffOverride(BaseModel):
    """Per-staff Step-4 override row (separate from Step 3's
    shift-swap overrides)."""

    model_config = ConfigDict(frozen=True)
    employee_id: str
    date: date_t
    max_flights: int | None = Field(default=None, ge=0, le=30)
    std_cutoff: time_t | None = None
    std_start: time_t | None = None
    is_newbie: bool = False


# 2026-05-24: four new override types for ad-hoc additions /
# removals of flights or staff on the planning day. Each row applies
# to D only, on top of whatever the SV portal + roster exports gave.
# The "stage-and-confirm" UX writes these into the workbook
# immediately on Save so the dashboard reflects the projected state
# (extra flight in the right ops-class card; extra body in the
# roster-by-shift block) BEFORE the assigner clicks Allocate.

class AddFlightOverride(BaseModel):
    """``type=add_flight`` — inject a new flight into the cleaned <class> list
    so it enters the allocation pool.

    Override columns used: flt, shift (used as STD HH:MM), dep, arr,
    ops_class. The assigner provides every field the cleaning step
    would have parsed from the SV portal — the engine treats this as
    if it had appeared in IN_SVportal."""

    model_config = ConfigDict(frozen=True)
    date: date_t
    flt: str
    dep: str = Field(min_length=3, max_length=4)
    arr: str = Field(min_length=3, max_length=4)
    std: time_t
    ops_class: OpsClass


class RemoveFlightOverride(BaseModel):
    """``type=remove_flight`` — pull a flight out of the allocation pool.

    Matches by (flt, std) tuple — same disambiguator used elsewhere for
    multi-leg rotations. Removed flights are dropped from
    the cleaned <class> list at apply time."""

    model_config = ConfigDict(frozen=True)
    date: date_t
    flt: str
    std: time_t


class AddStaffOverride(BaseModel):
    """``type=add_staff`` — inject a new (synthetic) staff into
    the availability list so they participate in this day's allocation.

    Override columns used: employee (name), shift (M/A/N/M1/A1 or
    M/ZC / A/ZC / ...), role (STAFF / AM / ZC). Engine assigns a
    synthetic employee_id of ``ADD_<n>`` so the schema's unique-ID
    rule isn't violated."""

    model_config = ConfigDict(frozen=True)
    date: date_t
    name: str = Field(min_length=1, max_length=80)
    shift: ShiftCode
    role: Role


class RemoveStaffOverride(BaseModel):
    """``type=remove_staff`` — pull a staff out of today's allocation.

    Semantically identical to ``type=sick`` (which already exists);
    kept as a separate type for UI clarity. Both flip the staff's
    ``assignable`` flag to False before Step 3 runs."""

    model_config = ConfigDict(frozen=True)
    date: date_t
    name: str = Field(min_length=1, max_length=80)


# ====================================================================
# Phase R — per-flight relaxation overrides (2026-05-15)
#
# These four override types let the assigner explicitly waive a hard
# constraint for ONE specific flight (or one specific (flight, staff)
# pair). Each row is read from the override list on D and the engine's
# eligibility / post-pass code checks the override set before applying
# the corresponding constraint.
#
# All four take effect only on the named date. Wiped by the standard
# reset_overrides flow.
# ====================================================================


class WaiveH10Pair(BaseModel):
    """Waive the H10 15-min spacing rule between ONE flight and ONE
    staff's other flight at a given STD.

    Override columns: type=waive_h10_pair, flight, std, employee,
    other_std. ``flight`` is the bare FLT number; ``std`` disambiguates
    multi-leg rotations.

    Example. flt 339 (STD 06:10) is unallocated because KARRA PRANAY
    REDDY already has flt 6305 at 06:15. A WaiveH10Pair row of:
        date=2026-05-06, flight=339, std=06:10,
        employee=KARRA PRANAY REDDY, other_std=06:15
    tells the solver: when building the H10 spacing constraint, EXCLUDE
    the pair (flt 339 @ 06:10, flt @ 06:15) from the no-overlap set
    for this staff.

    Granular by design — does NOT relax H10 for the staff's other
    flights; only this exact pair gets the waiver.
    """

    model_config = ConfigDict(frozen=True)
    date: date_t
    flight: str
    std: time_t
    employee_id: str
    other_std: time_t


class RaiseCapForFlight(BaseModel):
    """Per-staff one-shot cap raise to absorb ONE specific flight.

    Override columns: type=raise_cap, flight, std, employee.

    Example. ARSHI KHAN is at 24/24 cap. A RaiseCapForFlight row of:
        date=2026-05-06, flight=1185, std=06:10, employee=ARSHI KHAN
    raises ARSHI's H16 cap by 1 (to 25) **for this run only**. The cap
    reverts after the engine completes — operationally we're not
    permanently changing the cap; we're just letting one extra flight
    land on this person.
    """

    model_config = ConfigDict(frozen=True)
    date: date_t
    flight: str
    std: time_t
    employee_id: str


class SkipP2FBuffer(BaseModel):
    """Waive a P2F handler's D-3hr / D-1hr / D+20min buffer to allow ONE
    specific normal flight inside the window.

    Override columns: type=skip_p2f_buffer, flight, std, employee.

    Example. GAYATHRI is P2F-M handler. flt 6142 (08:35) falls in her
    D-3hr buffer (engine F6 hard-blocks). A SkipP2FBuffer row of:
        date=2026-05-06, flight=6142, std=08:35, employee=GAYATHRI
    tells eligibility F6 to skip the buffer check for this exact
    (flight, staff) pair.
    """

    model_config = ConfigDict(frozen=True)
    date: date_t
    flight: str
    std: time_t
    employee_id: str


class SkipINTLRemoval(BaseModel):
    """Tell the INTL post-pass NOT to remove a preceding flight when
    this specific INTL DEP flight gets allocated.

    Override columns: type=skip_intl_removal, flight, std.

    Example. flt 1064 lands on MANISH KUMAR. By default the post-pass
    removes MANISH's chronologically previous normal flight and
    redistributes. A SkipINTLRemoval row of:
        date=2026-05-06, flight=1064, std=10:25
    keeps both flights on MANISH. Used when the assigner explicitly
    decides the buffer isn't needed for this INTL.
    """

    model_config = ConfigDict(frozen=True)
    date: date_t
    flight: str
    std: time_t


class AllocationRow(BaseModel):
    """One allocation row. The display name and employee_id are
    both stored: names are for the human reader, employee_ids are for any
    downstream rejoin (e.g., the workload summary).

    Per user direction 2026-05-11: ``staff_*`` may be empty for
    "pre-plan only" flights (05:05–05:30 D+1 STDs whose actual fly-er
    is on D+1's M shift — but D+1's roster isn't loaded yet). Those
    rows carry a ``planned_by_*`` (the N-shift planner) and no staff.
    """

    model_config = ConfigDict(frozen=True)
    date: date_t
    flt: str
    dep: str
    arr: str
    std: time_t
    pax: int = Field(ge=0, le=600)
    staff_employee_id: str = ""
    staff_name: str = ""
    planned_by_employee_id: str | None = None
    planned_by_name: str | None = None
    relieved_by_employee_id: str | None = None
    relieved_by_name: str | None = None
    warning: str | None = None
    sheet_target: AllocationSheet
    # Color hint carried through from CleanFlightRow → solver → output.
    # Workbook writer applies cell fills; web readback returns the
    # flag so the browser tints rows yellow. Phase 3 (2026-05-14) made
    # is_international DEP-only; the pink-highlight machinery
    # (routing_via_excluded_hub) was deleted.
    is_international: bool = False


class WorkloadSummaryRow(BaseModel):
    """One row of OUT_Workload_Summary."""

    model_config = ConfigDict(frozen=True)
    employee_id: str
    name: str
    shift: ShiftCode | None
    role: Role
    target_preferred: int
    target_acceptable_max: int
    hard_cap: int
    actual: int
    deviation_below_preferred: int = Field(ge=0)
    deviation_above_preferred: int = Field(ge=0)
    violations: tuple[str, ...] = ()


class AllocationResult(BaseModel):
    """Top-level Step 4 output bundle returned by the orchestrator."""

    model_config = ConfigDict(frozen=True)
    rows: tuple[AllocationRow, ...]
    pairs: tuple[Pair, ...]
    summary: tuple[WorkloadSummaryRow, ...]
    warnings: tuple[WarningRow, ...]
    wall_clock_seconds: float = Field(ge=0)
    solver_status: str
    """OPTIMAL | FEASIBLE | INFEASIBLE | TIMEOUT — short-form status from
    the solver run."""


# ---------- Agent (Phase 1: write-action chassis, 2026-05-13) ----------
#
# These models are consumed only by ``src/agent/`` and the
# two new audit sheets in ``OUT_Agent_Trace`` / ``OUT_Agent_Queue``.
# Nothing in the existing pipeline (allocation, eligibility, post-passes,
# readers, writers, pairings, web UI) imports from this block — it's
# isolated by design so the chassis can land without engine impact.


class WriteAction(StrEnum):
    """The kind of action a single audit-trace row describes.

    READ                — agent read state via a read-only tool (kept here
                          so the same trace sheet works for Phase R too).
    WRITE_PREVIEW       — ApprovalGate.preview() built; nothing committed.
    APPROVAL_RECORDED   — human clicked Approve; token issued.
    APPROVAL_REJECTED   — human clicked Reject; gate dead.
    WRITE_APPLY         — agent applied the previously-approved change.
    ROLLBACK            — WriteTransaction restored from backup after a
                          mid-apply failure.
    """

    READ = "READ"
    WRITE_PREVIEW = "WRITE_PREVIEW"
    APPROVAL_RECORDED = "APPROVAL_RECORDED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    WRITE_APPLY = "WRITE_APPLY"
    ROLLBACK = "ROLLBACK"


class ApprovalStatus(StrEnum):
    """Lifecycle state of a single ApprovalGate, persisted in SQLite.

    PENDING   — preview built, awaiting human decision.
    APPROVED  — token issued, not yet applied or expired.
    APPLIED   — token consumed by a successful apply().
    REJECTED  — human said no.
    EXPIRED   — TTL elapsed before apply() was called.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class AgentTraceRow(BaseModel):
    """One row of the OUT_Agent_Trace sheet — the forensic log of every
    agent action.

    The sheet is append-only by design: ``reset_outputs`` excludes it so
    audit history survives Plan / Allocate runs. Manual purge via a
    separate "clear agent history" tool when the assigner explicitly
    wants it gone.

    Hash columns let an auditor confirm the pre/post state without
    storing entire workbook snapshots inline.
    """

    model_config = ConfigDict(frozen=True)
    timestamp: datetime_t
    txn_id: str
    """ULID-style identifier; ties together preview + approval + apply +
    (optional) rollback rows for the same logical action."""
    action_type: WriteAction
    tool_name: str
    """The agent tool that emitted this row (e.g. add_intl_airport,
    update_roster_cell, mark_unavailable)."""
    target: str
    """Plain-English description of what was touched
    (e.g. "configs/config.yml#international_airports", "override row 7")."""
    pre_state_hash: str = ""
    """SHA-256 hex digest of the affected file/sheet before the change.
    Empty string for READ rows."""
    post_state_hash: str = ""
    """SHA-256 hex digest of the affected file/sheet after the change.
    Empty string for READ and PREVIEW rows (no change yet)."""
    approval_user: str = ""
    """Empty for READ / PREVIEW / ROLLBACK; the human who clicked
    Approve/Reject for the approval-bound rows."""
    approval_timestamp: datetime_t | None = None
    note: str = ""
    """Free-form: rejection reason, rollback cause, plan-summary blurb, etc."""


class AgentQueueRow(BaseModel):
    """One row of the OUT_Agent_Queue sheet — items the agent flagged for
    human triage via the ``flag_for_human`` tool (CONTEXT.md §2.7).

    Kept separate from OUT_Agent_Trace so the assigner has a focused
    work-list view independent of the firehose of read/write events.
    """

    model_config = ConfigDict(frozen=True)
    timestamp: datetime_t
    queue_id: str
    """Stable identifier so the UI can mark rows resolved/dismissed
    without re-keying by row index."""
    question: str
    """The user's original prompt that triggered the escalation."""
    why_stuck: str
    """The agent's own statement of why it couldn't resolve confidently."""
    partial_evidence: str
    """JSON-serialized snapshot of what the agent had figured out so far
    (citations, partial findings). Read-only context for the human."""
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    status: Literal["OPEN", "RESOLVED", "DISMISSED"] = "OPEN"
    resolved_by: str = ""
    resolved_at: datetime_t | None = None
    resolution_note: str = ""
