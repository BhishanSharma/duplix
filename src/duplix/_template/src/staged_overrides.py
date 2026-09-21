"""Apply add_flight / remove_flight / add_staff / remove_staff /
change_role / sick overrides directly to the in-memory data, so the
dashboard reflects them BEFORE Allocate is clicked.

"Save (no re-run)" lets the assigner stage a change — add a person, add
a flight, pull someone out — and see the projected state on the
dashboard, confirming intent before triggering the solver. This module
is the bridge.

Apply rules:
  add_flight    → append a CleanFlightRow under the named ops_class.
  remove_flight → drop the matching (flt, std) row from every ops_class.
  add_staff     → append a synthetic AvailabilityRow with id ``ADD_NNN``.
  remove_staff  → flip the matching staff's ``assignable`` to False
                  (same semantics as type=sick).
  change_role   → rewrite the staff's role, and their status when the
                  new role is ZC.
  ZC list       → everyone on the saved Zone Controller list for D
                  (``zc_store``, picked on the dashboard) is promoted to
                  ZC, and anyone removed there is taken off ZC — even if
                  the roster cell still says ``/ZC``.

Every call re-applies ALL staged rows from scratch, which is why each
applier is written to be a no-op when its effect is already present.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date as date_t
from datetime import time as time_t
from typing import Any

from . import zc_store
from .schemas import (
    STATUS_IS_ZC,
    AvailabilityRow,
    CleanFlightRow,
    CrewStatus,
    OpsClass,
    OverrideType,
    Role,
)
from .state import AppState


def _cell(row: Mapping[str, Any], *names: str) -> str:
    """First non-blank value among ``names``, stripped. '' when none."""
    for n in names:
        v = row.get(n)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _parse_time(s: str) -> time_t | None:
    """Best-effort HH:MM parser. Accepts ``8:30`` / ``08:30`` /
    ``08:30:00``. None on failure (caller skips the row)."""
    if not s:
        return None
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return time_t(h, m)
    except (ValueError, IndexError):
        return None


def _parse_date(s: str, fallback: date_t) -> date_t:
    """Accept ISO (2026-05-28) or MM/DD (05/28, year from fallback)."""
    if not s:
        return fallback
    try:
        return date_t.fromisoformat(s)
    except ValueError:
        pass
    try:
        mm, dd = s.split("/")[:2]
        return date_t(fallback.year, int(mm), int(dd))
    except (ValueError, IndexError):
        return fallback


def _normalize_name(s: Any) -> str:
    """Collapse internal whitespace + uppercase, so 'AKSHAT  CHATURVEDI'
    and 'akshat chaturvedi' resolve to the same key."""
    import re as _re
    return _re.sub(r"\s+", " ", str(s or "")).strip().upper()


def apply_staged_overrides(state: AppState, d_day: date_t) -> dict[str, int]:
    """Apply every staged override type to the cleaned flights and the
    availability list. Returns {override_type: rows_applied}."""
    counts: dict[str, int] = {
        OverrideType.ADD_FLIGHT.value:    0,
        OverrideType.REMOVE_FLIGHT.value: 0,
        OverrideType.ADD_STAFF.value:     0,
        OverrideType.REMOVE_STAFF.value:  0,
        OverrideType.CHANGE_ROLE.value:   0,
        "zc_list":                        0,
    }
    with state.lock:
        for row in list(state.overrides):
            tp = _cell(row, "type").lower()
            if tp == OverrideType.ADD_FLIGHT.value:
                if _apply_add_flight(state, row, d_day):
                    counts[tp] += 1
            elif tp == OverrideType.REMOVE_FLIGHT.value:
                if _apply_remove_flight(state, row, d_day):
                    counts[tp] += 1
            elif tp == OverrideType.ADD_STAFF.value:
                if _apply_add_staff(state, row, d_day):
                    counts[tp] += 1
            elif tp in (OverrideType.REMOVE_STAFF.value, OverrideType.SICK.value):
                # sick is handled again by step3, but doing it here too
                # lets the dashboard reflect it pre-Allocate.
                if _apply_remove_staff(state, row, d_day):
                    counts[OverrideType.REMOVE_STAFF.value] += 1
            elif tp == OverrideType.CHANGE_ROLE.value:
                if _apply_change_role(state, row, d_day):
                    counts[tp] += 1
        # The dashboard's ZC list goes last, so an explicit change_role
        # row (e.g. ZC -> AM for someone who is unwell) is applied first
        # and the list only promotes people still in the live pool.
        counts["zc_list"] = _apply_zc_list(state, d_day)
    return counts


# ---------------- flights ----------------


def _apply_add_flight(state: AppState, row: Mapping[str, Any], d_day: date_t) -> bool:
    flt = _cell(row, "flight", "flt", "employee")
    dep = _cell(row, "dep").upper()[:3]
    arr = _cell(row, "arr").upper()[:3]
    std = _parse_time(_cell(row, "std", "shift"))
    if not (flt and dep and arr and std):
        return False
    try:
        ops_class = OpsClass(_cell(row, "ops_class").lower() or "day")
    except ValueError:
        return False
    flight_date = _parse_date(_cell(row, "date"), d_day)
    try:
        pax = int(float(_cell(row, "pax", "limit") or 0))
    except (TypeError, ValueError):
        pax = 0

    bucket = state.cleaned.setdefault(ops_class, [])
    # Idempotent: same date + flt + std on the same class is a no-op.
    for existing in bucket:
        if (existing.date == flight_date and existing.flt == flt
                and existing.std == std):
            return False
    try:
        new_row = CleanFlightRow(
            date=flight_date,
            flt=flt,
            type=_cell(row, "ac_type"),
            ac=_cell(row, "ac"),
            dep=dep,
            arr=arr,
            std=std,
            load=max(0, min(600, pax)),
            ops_class=ops_class,
        )
    except ValueError as exc:
        print(f"  [add_flight] skipped {flt!r}: {exc}")
        return False
    bucket.append(new_row)
    return True


def _apply_remove_flight(state: AppState, row: Mapping[str, Any], d_day: date_t) -> bool:
    flt = _cell(row, "flight", "flt", "employee")
    std = _parse_time(_cell(row, "std", "shift"))
    if not (flt and std):
        return False
    # Optional matchers — narrow the match when the form supplied them.
    want_dep = _cell(row, "dep").upper()[:3]
    want_arr = _cell(row, "arr").upper()[:3]
    raw_date = _cell(row, "date")
    want_date = _parse_date(raw_date, d_day) if raw_date else None
    # An MM/DD date has no year, so match on month+day only.
    day_only = bool(raw_date and "/" in raw_date)

    removed = False
    for ops, rows in state.cleaned.items():
        keep: list[CleanFlightRow] = []
        for r in rows:
            match = (
                r.flt == flt
                and r.std == std
                and (not want_dep or r.dep == want_dep)
                and (not want_arr or r.arr == want_arr)
            )
            if match and want_date is not None:
                if day_only:
                    match = (r.date.month, r.date.day) == (want_date.month, want_date.day)
                else:
                    match = r.date == want_date
            if match:
                removed = True
            else:
                keep.append(r)
        state.cleaned[ops] = keep
    return removed


# ---------------- staff ----------------


def _apply_add_staff(state: AppState, row: Mapping[str, Any], d_day: date_t) -> bool:
    name = _cell(row, "employee")
    shift = _cell(row, "shift").upper()
    role_raw = _cell(row, "role").upper() or "STAFF"
    if not (name and shift):
        print(f"  [add_staff] skipped: missing field — name={name!r}, shift={shift!r}")
        return False
    if shift not in ("M", "A", "N", "M1", "A1"):
        print(
            f"  [add_staff] skipped: shift {shift!r} not in "
            "(M, A, N, M1, A1) — use a plain shift code"
        )
        return False
    try:
        role = Role(role_raw)
    except ValueError:
        print(f"  [add_staff] skipped: role {role_raw!r} not in (STAFF, ZC, AM)")
        return False

    target = _normalize_name(name)
    d_iso = d_day.isoformat()
    for av in state.availability:
        if _normalize_name(av.name) == target and av.date.isoformat() == d_iso:
            return False  # already present — nothing to add

    n_added = sum(1 for av in state.availability if av.employee_id.startswith("ADD_"))
    state.availability.append(AvailabilityRow(
        employee_id=f"ADD_{n_added + 1:03d}",
        name=name,
        role=role,
        date=d_day,
        status=CrewStatus(shift),
        raw_status=shift,
        assignable=True,
        current_shift=shift,
        license=None,
    ))
    return True


def _apply_remove_staff(state: AppState, row: Mapping[str, Any], d_day: date_t) -> bool:
    name = _cell(row, "employee")
    if not name:
        print("  [remove_staff] skipped: blank employee field")
        return False
    target = _normalize_name(name)
    d_iso = d_day.isoformat()
    found = False
    candidates: list[str] = []
    for i, av in enumerate(state.availability):
        if av.date.isoformat() != d_iso:
            continue
        nm = _normalize_name(av.name)
        candidates.append(nm)
        if nm == target:
            state.availability[i] = av.model_copy(update={"assignable": False})
            found = True
    if not found:
        # Loud diagnostic — common causes are a name typo, the staff not
        # being on shift today, or the wrong date.
        nearby = (
            [n for n in candidates if target.split()[0] in n]
            if " " in target else []
        )
        hint = f"; similar names on {d_iso}: {nearby[:3]}" if nearby else ""
        print(
            f"  [remove_staff] WARNING: no roster row matches "
            f"employee={name!r} on {d_iso} ({len(candidates)} staff rows "
            f"for that date){hint}"
        )
    return found


def _apply_change_role(state: AppState, row: Mapping[str, Any], d_day: date_t) -> bool:
    """Flip a staff's role so the dashboard roster reflects the change
    without a re-solve.

    The ``shift`` column carries the NEW role (STAFF / ZC / AM). For ZC
    we also rewrite status / raw_status to the ``<shift>/ZC`` variant so
    downstream readers see a ZC-flagged row; AM drops them out of the
    live pool, matching the step3 semantics.
    """
    name = _cell(row, "employee")
    new_role_raw = _cell(row, "shift", "role").upper()
    if not (name and new_role_raw):
        return False
    try:
        new_role = Role(new_role_raw)
    except ValueError:
        return False

    target = _normalize_name(name)
    d_iso = d_day.isoformat()
    found = False
    for i, av in enumerate(state.availability):
        if _normalize_name(av.name) != target or av.date.isoformat() != d_iso:
            continue
        update: dict[str, Any] = {"role": new_role}
        if new_role is Role.ZC:
            update = _zc_update(av)
        elif new_role is Role.AM:
            update["assignable"] = False
        state.availability[i] = av.model_copy(update=update)
        found = True
    return found


# ---------------- Zone Controller list ----------------

# A P2F handler who is made ZC keeps the handler duty: M/P2F -> M/P2F/ZC.
# (Rewriting it to plain M/ZC would drop them from the P2F auto-pick.)
_P2F_TO_ZC_P2F: dict[CrewStatus, CrewStatus] = {
    CrewStatus.P2F_M: CrewStatus.ZC_P2F_M,
    CrewStatus.P2F_A: CrewStatus.ZC_P2F_A,
    CrewStatus.P2F_N: CrewStatus.ZC_P2F_N,
}


def _zc_update(av: AvailabilityRow) -> dict[str, Any]:
    """The field changes that make ``av`` a Zone Controller for the day:
    role ZC plus the matching ``<shift>/ZC`` status, so every downstream
    reader (dashboard, step3, auto-P2F picker) sees a ZC-flagged row."""
    update: dict[str, Any] = {"role": Role.ZC}
    if av.status in STATUS_IS_ZC:
        return update
    target = _P2F_TO_ZC_P2F.get(av.status)
    if target is None and av.current_shift in ("M", "A", "N", "M1", "A1"):
        try:
            target = CrewStatus(f"{av.current_shift}/ZC")
        except ValueError:
            target = None
    if target is not None:
        update["status"] = target
        update["raw_status"] = target.value
    return update


def _demote_update(av: AvailabilityRow) -> dict[str, Any]:
    """Undo a ZC promotion: back to the roster the person came from
    (STAFF / AM) and to the plain shift or P2F-handler status without
    the ``/ZC`` (M/ZC -> M, M/P2F/ZC -> M/P2F)."""
    update: dict[str, Any] = {"role": av.origin_role or Role.STAFF}
    lit = av.status.value
    if lit.endswith("/ZC"):
        lit = lit[: -len("/ZC")]
        target: CrewStatus | None = None
        try:
            target = CrewStatus(lit)
        except ValueError:
            # e.g. M1/P2F has no status of its own: keep just the shift.
            if lit.endswith("/P2F"):
                try:
                    target = CrewStatus(lit[: -len("/P2F")])
                except ValueError:
                    target = None
        if target is not None:
            update["status"] = target
            update["raw_status"] = target.value
    return update


def _apply_zc_list(state: AppState, d_day: date_t) -> int:
    """Make D's Zone Controllers match the dashboard's list. Returns how
    many rows on D are ZC because of the list.

    * Everyone on the saved list who is rostered on a shift and
      assignable on D is promoted. A name carried over from yesterday who
      is off today is simply skipped, not an error.
    * Everyone recorded as *removed* for D is taken back off ZC — this is
      how a ``/ZC`` that is still in the roster file gets overridden.

    Idempotent, like every applier here.
    """
    names, _source, _ = zc_store.effective(d_day)
    removed = {_normalize_name(n) for n in zc_store.removed(d_day)}
    d_iso = d_day.isoformat()
    if not any(av.date.isoformat() == d_iso for av in state.availability):
        return 0
    # The list this plan actually runs with becomes that day's own list,
    # so tomorrow's carry-over starts from it.
    zc_store.freeze(d_day)
    wanted = {_normalize_name(n) for n in names} - removed
    applied = 0
    for i, av in enumerate(state.availability):
        if av.date.isoformat() != d_iso:
            continue
        key = _normalize_name(av.name)
        if key in removed:
            if av.role is Role.ZC or av.status in STATUS_IS_ZC:
                state.availability[i] = av.model_copy(update=_demote_update(av))
            continue
        if key not in wanted:
            continue
        if not av.assignable or not av.current_shift:
            continue
        state.availability[i] = av.model_copy(update=_zc_update(av))
        applied += 1
    return applied
