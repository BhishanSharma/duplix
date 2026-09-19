"""Step 2 — extract roster availability (plan §10 Phase 2).

Reads the two uploaded rosters (regular staff + AM/ZC), normalises
statuses, and stores the long-format availability matrix in
``state.availability``. Coverage-shortfall warnings (W010, W011) land in
``state.warnings``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date as date_t
from datetime import timedelta
from pathlib import Path
from typing import cast

from .config import Config, load_config
from .io.readers import (
    DuplicateEmployeeIdError,
    read_am_roster,
    read_staff_roster,
    workbook_from_bytes,
)
from .schemas import (
    NON_ASSIGNABLE,
    SHIFT_CODES,
    AMRosterRow,
    AvailabilityRow,
    CrewRosterRow,
    CrewStatus,
    Role,
    Severity,
    ShiftCode,
    WarningRow,
)

# 2026-05-27 (centralization): the per-status shift mapping now lives
# in schemas.STATUS_TO_SHIFT (single source of truth). This local alias
# is kept ONLY for readability at call sites that already say
# `_STATUS_TO_SHIFT.get(...)` — its content is byte-identical to the
# central map. When a new status is added to CrewStatus, update the
# central STATUS_TO_SHIFT and every consumer picks it up — no more
# scattered duplicates to keep in sync.
from .schemas import STATUS_TO_SHIFT as _STATUS_TO_SHIFT

# Status values that ALSO imply a P2F license, even if the License column
# 2026-05-27 (centralization): the set of P2F-license-granting statuses
# now lives in schemas.P2F_LICENSE_GRANTING_STATUSES. The alias is kept
# so existing call sites read clearly. Add a new combined ZC+P2F or
# plain P2F status to the central STATUS_TO_P2F_HANDLER_SHIFT map and
# this set picks it up automatically.
from .schemas import P2F_LICENSE_GRANTING_STATUSES as _P2F_HANDLER_STATUSES
from .state import AppState


def _emit_for_window(
    rows: Iterable[CrewRosterRow | AMRosterRow],
    window: list[date_t],
) -> list[AvailabilityRow]:
    out: list[AvailabilityRow] = []
    for crew in rows:
        for d in window:
            if d not in crew.status_by_date:
                continue
            status = crew.status_by_date[d]
            raw = crew.raw_status_by_date.get(d, "")
            # Derive current_shift from any shift-bearing status: plain
            # shifts (M/A/N/M1/A1), ZC variants (M/ZC etc.), and P2F-
            # handler variants (P2F/M etc.). Older code only matched the
            # plain shifts via SHIFT_CODES, which left ZC/P2F rows with
            # current_shift=None — Step 3 then skipped them entirely.
            current_shift: ShiftCode | None = _STATUS_TO_SHIFT.get(status)
            if current_shift is None and status.value in SHIFT_CODES:
                # Defensive fallback for any new shift literal added to
                # CrewStatus without an entry in the map above.
                current_shift = cast(ShiftCode, status.value)
            # P2F-handler statuses auto-populate the license column so
            # downstream eligibility (F3 + F4) treats them as P2F-
            # licensed even when the assigner didn't set the License
            # cell explicitly.
            license_val = crew.license
            if status in _P2F_HANDLER_STATUSES and not (
                license_val and "P2F" in license_val.upper()
            ):
                license_val = "P2F (auto from roster cell)"
            # 2026-05-23: per-day role override. Previously crew.role
            # was inferred at READ time across the entire row's date
            # window — so an AM who was M/ZC on one day got tagged
            # Role.ZC for EVERY day, including days they were just
            # plain M. The bug surfaced as "AMs showing up as ZC" in
            # the dashboard. Fix: derive role per-day from THIS day's
            # status only; fall back to the crew's sheet-origin role.
            day_role = crew.role
            if status in (
                CrewStatus.ZC_M, CrewStatus.ZC_A, CrewStatus.ZC_N,
                CrewStatus.ZC_M1, CrewStatus.ZC_A1,
                # 2026-05-26: combined ZC+P2F cells also imply ZC role.
                CrewStatus.ZC_P2F_M, CrewStatus.ZC_P2F_A, CrewStatus.ZC_P2F_N,
                CrewStatus.ZC_P2F_M1, CrewStatus.ZC_P2F_A1,
            ):
                day_role = Role.ZC
            elif crew.role == Role.ZC:
                # Row-level inferred ZC but TODAY's cell isn't /ZC —
                # treat as AM (sheet origin = IN_AM_Roster).
                day_role = Role.AM
            out.append(AvailabilityRow(
                employee_id=crew.employee_id,
                name=crew.name,
                role=day_role,
                date=d,
                status=status,
                raw_status=raw,
                assignable=status not in NON_ASSIGNABLE,
                current_shift=current_shift,
                license=license_val,
            ))
    return out


def _coverage_warning(
    code: str,
    label: str,
    rows_loaded: int,
    covered_dates: set[date_t],
    target_window: list[date_t],
) -> WarningRow | None:
    if rows_loaded == 0:
        return None
    missing = [d for d in target_window if d not in covered_dates]
    if not missing:
        return None
    iso = ", ".join(d.isoformat() for d in missing)
    return WarningRow(
        severity=Severity.WARN,
        code=code,
        message=(
            f"{label} roster does not cover {iso}; "
            "emitting empty availability for those dates."
        ),
    )


def extract_availability(
    staff_rows: list[CrewRosterRow],
    am_rows: list[AMRosterRow],
    d_day: date_t,
) -> tuple[list[AvailabilityRow], list[WarningRow]]:
    window: list[date_t] = [d_day, d_day + timedelta(days=1)]

    staff_dates: set[date_t] = set()
    for s in staff_rows:
        staff_dates.update(s.status_by_date.keys())
    am_dates: set[date_t] = set()
    for a in am_rows:
        am_dates.update(a.status_by_date.keys())

    warnings: list[WarningRow] = []
    w_staff = _coverage_warning("W010", "staff", len(staff_rows), staff_dates, window)
    if w_staff is not None:
        warnings.append(w_staff)
    w_am = _coverage_warning("W011", "AM", len(am_rows), am_dates, window)
    if w_am is not None:
        warnings.append(w_am)

    avail = _emit_for_window(staff_rows, window) + _emit_for_window(am_rows, window)
    return avail, warnings


def run(
    state: AppState,
    d_day: date_t,
    config_path: Path | str = "configs/config.yml",
    *,
    append_warnings: bool = False,
) -> dict[str, int]:
    """Read both uploaded rosters and store availability + warnings on
    ``state``. Returns a per-role count dict plus WARNINGS."""
    config: Config = load_config(config_path)
    wb_staff = workbook_from_bytes(state.input_bytes("staff_roster"))
    wb_am = workbook_from_bytes(state.input_bytes("am_roster"))
    avail: list[AvailabilityRow] = []
    warnings: list[WarningRow] = []
    try:
        try:
            staff_rows = read_staff_roster(wb_staff, config)
            am_rows = read_am_roster(wb_am, config)
        except DuplicateEmployeeIdError as e:
            # W020: Surface as ERROR and abort.
            warnings = [WarningRow(
                severity=Severity.ERROR,
                code="W020",
                message=str(e),
            )]
            _store_warnings(state, warnings, append=append_warnings)
            return {"WARNINGS": len(warnings), "ABORTED": 1}
        # 2026-05-27 (user direction): also check for CROSS-roster ID
        # collisions. _check_unique_employee_ids only validates within
        # one sheet at a time — if the same employee_id appears in
        # BOTH Staff_Roster and AM_Roster, downstream code collapses
        # the two rows into one decision variable in the solver. The
        # M1/ZC entry that "doesn't get extracted" is actually
        # silently overwritten by the same-id row from the other
        # sheet. Abort with a clear message.
        cross_dup_ids: list[tuple[str, str, str]] = []   # (id, staff_name, am_name)
        staff_id_to_name = {r.employee_id: r.name for r in staff_rows}
        for r in am_rows:
            if r.employee_id in staff_id_to_name:
                cross_dup_ids.append(
                    (r.employee_id, staff_id_to_name[r.employee_id], r.name)
                )
        if cross_dup_ids:
            previews = "; ".join(
                f"id={eid!r}: Staff={sn!r}, AM={an!r}"
                for eid, sn, an in cross_dup_ids[:5]
            )
            if len(cross_dup_ids) > 5:
                previews += f", … (+{len(cross_dup_ids) - 5} more)"
            warnings = [WarningRow(
                severity=Severity.ERROR,
                code="W022",
                message=(
                    f"{len(cross_dup_ids)} employee_id(s) appear in BOTH "
                    f"the staff roster AND the AM roster: {previews}. Each "
                    "staff must have a UNIQUE id across both sheets — the "
                    "solver keys decision variables by id, so duplicates "
                    "silently drop one of the rows (the M1/ZC entry the "
                    "assigner expected to see often disappears this way). "
                    "Fix the IDs in the source files, re-upload, re-run."
                ),
            )]
            _store_warnings(state, warnings, append=append_warnings)
            return {"WARNINGS": len(warnings), "ABORTED": 1}
        # 2026-05-27 (informational): same NAME with DIFFERENT IDs
        # across rosters is allowed (it's a real scenario — different
        # people with the same name), but emit a WARN so the assigner
        # knows to be careful when matching override rows by name.
        staff_names_lower = {r.name.strip().upper(): r.employee_id for r in staff_rows}
        same_name_diff_id: list[tuple[str, str, str]] = []  # (name, staff_id, am_id)
        for r in am_rows:
            key = r.name.strip().upper()
            if key in staff_names_lower and staff_names_lower[key] != r.employee_id:
                same_name_diff_id.append((r.name, staff_names_lower[key], r.employee_id))
        # Also same name within Staff sheet (different IDs already
        # caught for the same sheet by _check_unique_employee_ids on
        # ids — but two different ids with the SAME name slip past).
        staff_name_counts: dict[str, list[str]] = {}
        for r in staff_rows:
            staff_name_counts.setdefault(r.name.strip().upper(), []).append(r.employee_id)
        dup_names_in_staff = [
            (name, ids) for name, ids in staff_name_counts.items() if len(ids) > 1
        ]
        cross_warns: list[WarningRow] = []
        if same_name_diff_id:
            previews = "; ".join(
                f"{nm!r} (Staff id={sid}, AM id={aid})"
                for nm, sid, aid in same_name_diff_id[:5]
            )
            cross_warns.append(WarningRow(
                severity=Severity.WARN,
                code="W023",
                message=(
                    f"{len(same_name_diff_id)} name(s) appear in BOTH "
                    f"rosters with different IDs: {previews}. Override "
                    "rows that match by name will resolve to the AM-roster "
                    "ID (later wins). Use IDs explicitly in overrides to "
                    "be unambiguous."
                ),
            ))
        if dup_names_in_staff:
            previews = "; ".join(
                f"{nm!r} (ids: {', '.join(ids)})"
                for nm, ids in dup_names_in_staff[:5]
            )
            cross_warns.append(WarningRow(
                severity=Severity.WARN,
                code="W024",
                message=(
                    f"{len(dup_names_in_staff)} name(s) appear MORE THAN "
                    f"ONCE in the staff roster with distinct IDs: {previews}. "
                    "Override rows that match by name will resolve to "
                    "the LAST-seen ID. Disambiguate via IDs in overrides."
                ),
            ))
        avail, warnings = extract_availability(staff_rows, am_rows, d_day)
        warnings = cross_warns + warnings
        with state.lock:
            state.availability = avail
        _store_warnings(state, warnings, append=append_warnings)
    finally:
        wb_staff.close()
        wb_am.close()

    # Re-apply the staged drawer mutations AFTER availability has been
    # rebuilt from the raw rosters. Without this, every Plan wipes the
    # assigner's staged intent (remove_staff flipping assignable=False,
    # add_staff synthetic rows, add_flight / remove_flight) and the
    # solver sees the original roster instead. The override list is the
    # source of truth; cleaned flights + availability are working
    # copies that get patched here on every step2 run.
    from . import staged_overrides as _so
    applied = _so.apply_staged_overrides(state, d_day)
    total = sum(applied.values()) if applied else 0
    if total:
        print(
            "  staged overrides re-applied after roster rebuild: "
            + ", ".join(f"{k}={v}" for k, v in applied.items() if v)
        )

    counts: dict[str, int] = {}
    for r in avail:
        counts[r.role.value] = counts.get(r.role.value, 0) + 1
    counts["WARNINGS"] = len(warnings)
    return counts


def _store_warnings(
    state: AppState, warnings: list[WarningRow], *, append: bool,
) -> None:
    """Put this stage's warnings on the state, either appending to what
    a previous stage left or replacing it."""
    with state.lock:
        state.warnings = (list(state.warnings) + warnings) if append else list(warnings)
