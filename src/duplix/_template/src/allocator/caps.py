"""Per-(shift, role) workload bands — defaults from JSON, plus
iteration-scoped overrides.

Task 2 (2026-05-12) replaces the previous hardcoded ``(Role, "day"|"night")``
tables with a per-shift-role JSON config at ``configs/shift_limits.json``.
Each (shift, role) entry has three values:

  min     — S1 preferred floor; counts below this incur shortfall penalty
  target  — S1 acceptable max; counts above this start to penalize
  max     — H16 hard cap; counts above this are infeasible

Validation: ``min <= target <= max`` and all positive integers.

The same JSON powers the override drawer's "Edit shift limits" form
(see web/overrides.py). Per-iteration overrides are pushed into
``_iteration_overrides`` by the drawer or by override rows; they
shadow the JSON defaults without persisting to disk. ``reset_overrides()``
clears them and is invoked on the engine's reset path.

Per-staff overrides:
  * a ``max_flights`` override row LOWERS a single staff's H16 cap.
  * a ``preferred_target`` override row REPLACES a single staff's
    preferred (default = (shift, role)'s ``min``).
  Both reset when the overrides are cleared.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterable
from pathlib import Path

from ..schemas import Role, StaffMember

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "shift_limits.json"
# Per-iteration overrides persist here so they survive the process
# boundary between the web server (where the drawer writes them) and
# the CLI subprocess that actually runs the engine. Wiped by
# ``reset_overrides()`` and by the engine's reset path.
_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "state" / "iteration_overrides.json"

# All known shift codes — kept in sync with config.status.shifts.
_KNOWN_SHIFTS = ("M", "A", "N", "M1", "A1")
_ROLE_KEYS = {Role.STAFF: "STAFF", Role.ZC: "ZC"}

# Cached default table loaded from JSON. Refreshed on import and on any
# explicit ``reload_defaults()`` call (e.g., after the drawer edits the
# defaults rather than the iteration scratch).
_defaults: dict[tuple[str, Role], dict[str, int]] = {}
# Per-iteration shadow values: empty until the drawer writes here.
_iteration_overrides: dict[tuple[str, Role], dict[str, int]] = {}
# Per-staff iteration overrides — keyed by employee_id.
_iteration_per_staff_preferred: dict[str, int] = {}
_iteration_per_staff_cap: dict[str, int] = {}
_lock = threading.RLock()


def _coerce_int(v: object, key: str) -> int:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{key} must be a number, got {v!r}")
    iv = int(v)
    if iv <= 0:
        raise ValueError(f"{key} must be a positive integer, got {iv}")
    return iv


def _validate_band(min_v: int, target_v: int, max_v: int, where: str) -> None:
    """Reject any (min, target, max) triple that breaks the ordering
    invariant. Single source of truth used by both the JSON loader and
    the drawer write path so a bad form submission can't slip through."""
    if not (min_v <= target_v <= max_v):
        raise ValueError(
            f"{where}: require min <= target <= max, got "
            f"min={min_v} target={target_v} max={max_v}"
        )


def _load_defaults_from_disk() -> dict[tuple[str, Role], dict[str, int]]:
    raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    shifts_block = raw.get("shifts", {})
    out: dict[tuple[str, Role], dict[str, int]] = {}
    for shift, role_map in shifts_block.items():
        if shift not in _KNOWN_SHIFTS:
            continue
        for role_name, vals in role_map.items():
            role = Role.STAFF if role_name == "STAFF" else (
                Role.ZC if role_name == "ZC" else None
            )
            if role is None:
                continue
            min_v = _coerce_int(vals.get("min"), f"{shift}/{role_name}.min")
            target_v = _coerce_int(vals.get("target"), f"{shift}/{role_name}.target")
            max_v = _coerce_int(vals.get("max"), f"{shift}/{role_name}.max")
            _validate_band(min_v, target_v, max_v, f"{shift}/{role_name}")
            out[(shift, role)] = {"min": min_v, "target": target_v, "max": max_v}
    return out


def reload_defaults() -> None:
    """Re-read ``configs/shift_limits.json`` into the module cache.

    Use this after editing the file out-of-band; the drawer's per-iteration
    overrides write through ``set_iteration_band`` instead — those land
    in ``data/state/iteration_overrides.json`` rather than the defaults
    file."""
    global _defaults
    with _lock:
        _defaults = _load_defaults_from_disk()


def _state_path_or_none() -> Path | None:
    """Return the runtime-state file path, or None if its parent
    directory cannot be created. Callers treat None as "no persistence
    available" and skip the disk hop."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return _STATE_PATH


def _write_iteration_state_to_disk() -> None:
    """Serialize the in-memory iteration overrides to JSON so the engine
    subprocess sees the same shadow when the user clicks Allocate."""
    p = _state_path_or_none()
    if p is None:
        return
    payload = {
        "shift_role": [
            {"shift": shift, "role": (role.value if hasattr(role, "value") else role),
             "min": v["min"], "target": v["target"], "max": v["max"]}
            for (shift, role), v in _iteration_overrides.items()
        ],
        "per_staff_preferred": dict(_iteration_per_staff_preferred),
        "per_staff_cap": dict(_iteration_per_staff_cap),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_iteration_state_from_disk() -> None:
    """Populate the in-memory iteration overrides from the state file
    on module import. Silently skips when the file is absent (= no
    overrides for this iteration)."""
    if not _STATE_PATH.exists():
        return
    try:
        payload = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for entry in payload.get("shift_role", []):
        shift = entry.get("shift")
        role_name = entry.get("role")
        role = Role.STAFF if role_name == "STAFF" else (
            Role.ZC if role_name == "ZC" else None
        )
        if shift not in _KNOWN_SHIFTS or role is None:
            continue
        try:
            min_v = _coerce_int(entry.get("min"), "min")
            target_v = _coerce_int(entry.get("target"), "target")
            max_v = _coerce_int(entry.get("max"), "max")
            _validate_band(min_v, target_v, max_v, f"{shift}/{role_name}")
        except ValueError:
            continue
        _iteration_overrides[(shift, role)] = {
            "min": min_v, "target": target_v, "max": max_v,
        }
    for emp_id, v in payload.get("per_staff_preferred", {}).items():
        with contextlib.suppress(ValueError, TypeError):
            _iteration_per_staff_preferred[str(emp_id)] = int(v)
    for emp_id, v in payload.get("per_staff_cap", {}).items():
        with contextlib.suppress(ValueError, TypeError):
            _iteration_per_staff_cap[str(emp_id)] = int(v)


# Load once at import. Module remains usable even if the file's missing
# at import time — we'll fall back to a minimal hardcoded table so unit
# tests that don't ship the JSON don't crash the import.
try:
    _defaults = _load_defaults_from_disk()
except (FileNotFoundError, json.JSONDecodeError, ValueError):
    _defaults = {
        ("M",  Role.STAFF): {"min": 22, "target": 23, "max": 24},
        ("M",  Role.ZC):    {"min": 14, "target": 15, "max": 16},
        ("A",  Role.STAFF): {"min": 22, "target": 23, "max": 24},
        ("A",  Role.ZC):    {"min": 14, "target": 15, "max": 16},
        ("M1", Role.STAFF): {"min": 22, "target": 23, "max": 24},
        ("M1", Role.ZC):    {"min": 14, "target": 15, "max": 16},
        ("A1", Role.STAFF): {"min": 22, "target": 23, "max": 24},
        ("A1", Role.ZC):    {"min": 14, "target": 15, "max": 16},
        ("N",  Role.STAFF): {"min": 20, "target": 21, "max": 22},
        ("N",  Role.ZC):    {"min": 10, "target": 11, "max": 12},
    }

# Pick up any iteration overrides left over from a prior session
# (e.g., user closed the browser without clicking Reset).
_load_iteration_state_from_disk()


# ---------- public API ----------

def get_band(shift: str, role: Role) -> dict[str, int] | None:
    """Return ``{"min", "target", "max"}`` for (shift, role), preferring
    any active iteration override over the JSON default. ``None`` when
    no band exists (unknown shift or role)."""
    with _lock:
        if (shift, role) in _iteration_overrides:
            return dict(_iteration_overrides[(shift, role)])
        if (shift, role) in _defaults:
            return dict(_defaults[(shift, role)])
    return None


def set_iteration_band(
    shift: str, role: Role, *, min_v: int, target_v: int, max_v: int,
) -> dict[str, int]:
    """Install a per-iteration override for the (shift, role) band. The
    drawer "Edit shift limits" form is the only intended caller. Returns
    the persisted dict for confirmation.

    Validates via ``_coerce_int`` (positive integer) AND ``_validate_band``
    (ordering); raises ``ValueError`` on either failure.
    Also written to ``data/state/iteration_overrides.json`` so a band
    tweak survives a server restart. Reset clears it.
    """
    role_name = role.value if hasattr(role, "value") else role
    where = f"{shift}/{role_name}"
    min_i = _coerce_int(min_v, f"{where}.min")
    target_i = _coerce_int(target_v, f"{where}.target")
    max_i = _coerce_int(max_v, f"{where}.max")
    _validate_band(min_i, target_i, max_i, where)
    with _lock:
        _iteration_overrides[(shift, role)] = {
            "min": min_i, "target": target_i, "max": max_i,
        }
        _write_iteration_state_to_disk()
        return dict(_iteration_overrides[(shift, role)])


def set_iteration_per_staff_preferred(employee_id: str, value: int) -> None:
    """Per-staff iteration override for the preferred-target. Used by
    the engine when an an override row of type=preferred_flights is
    present. ``reset_overrides()`` clears this too."""
    with _lock:
        _iteration_per_staff_preferred[str(employee_id)] = int(value)
        _write_iteration_state_to_disk()


def set_iteration_per_staff_cap(employee_id: str, value: int) -> None:
    """Per-staff iteration override that LOWERS the hard cap (matches
    the legacy ``max_flights`` override semantics)."""
    with _lock:
        _iteration_per_staff_cap[str(employee_id)] = int(value)
        _write_iteration_state_to_disk()


def reset_overrides() -> None:
    """Wipe every iteration-scoped override (both per-(shift, role) and
    per-staff). Engine's reset path and the drawer "Reset" button both
    call this so a clean run uses the JSON defaults. Also removes the
    on-disk state file so a later run can't pick up stale overrides."""
    with _lock:
        _iteration_overrides.clear()
        _iteration_per_staff_preferred.clear()
        _iteration_per_staff_cap.clear()
        with contextlib.suppress(FileNotFoundError):
            _STATE_PATH.unlink()


def snapshot_iteration_overrides() -> dict[str, dict[str, dict[str, int]]]:
    """Diagnostic snapshot for the drawer's "show current iteration
    overrides" hint and for the audit harness. Read-only copy.

    Shape:
      {
        "shift_role": {"M/STAFF": {"min": ..., "target": ..., "max": ...}, ...},
        "per_staff_preferred": {"<emp_id>": 20, ...},
        "per_staff_cap":       {"<emp_id>": 18, ...}
      }
    """
    with _lock:
        return {
            "shift_role": {
                f"{shift}/{(role.value if hasattr(role, 'value') else role)}": dict(v)
                for (shift, role), v in _iteration_overrides.items()
            },
            "per_staff_preferred": dict(_iteration_per_staff_preferred),
            "per_staff_cap": dict(_iteration_per_staff_cap),
        }


# ---------- engine-facing helpers (used by solver, postsolve, post-passes) ----------

def hard_cap_for(staff: StaffMember) -> int:
    """Return the binding upper bound on flights for ``staff`` today.

    Order of precedence:
      1. Per-staff iteration override (LOWER only)
      2. StaffMember.max_flights_cap from an override (LOWER only)
      3. JSON default for (shift, role)

    AMs and off-shift staff get 0 (they don't fly).
    """
    if staff.role == Role.AM or staff.shift_today is None:
        return 0
    band = get_band(staff.shift_today, staff.role)
    if band is None:
        return 0
    base = band["max"]
    # Per-staff iteration override (only lowers).
    iter_cap = _iteration_per_staff_cap.get(staff.employee_id)
    if iter_cap is not None:
        base = min(base, iter_cap)
    # Legacy max_flights override cap (only lowers).
    if staff.max_flights_cap is not None:
        base = min(base, staff.max_flights_cap)
    return base


def aggregate_capacity(staff: Iterable[StaffMember]) -> int:
    """Sum of ``hard_cap_for()`` across all input staff. Compared against
    flight count before invoking the solver — if capacity < demand the
    day is structurally infeasible regardless of solver strategy."""
    return sum(hard_cap_for(s) for s in staff)


def preferred_target_for(staff: StaffMember) -> int:
    """S1 preferred count for ``staff`` today.

    Order of precedence:
      1. Per-staff iteration override
      2. JSON default for (shift, role)
    """
    if staff.role == Role.AM or staff.shift_today is None:
        return 0
    iter_pref = _iteration_per_staff_preferred.get(staff.employee_id)
    if iter_pref is not None:
        return int(iter_pref)
    band = get_band(staff.shift_today, staff.role)
    return band["min"] if band else 0


def acceptable_max_for(staff: StaffMember) -> int:
    """Upper end of the acceptable range."""
    if staff.role == Role.AM or staff.shift_today is None:
        return 0
    band = get_band(staff.shift_today, staff.role)
    return band["target"] if band else 0


def preferred_target_for_shift_role(shift: str, role: Role) -> int:
    """Aggregate preferred lookup keyed purely on (shift, role).

    Used by the planner / readback layer for the "people needed" display
    on the dashboard (Task 2a) — the per-staff override path doesn't
    apply because we're calculating a shift-wide capacity estimate, not
    a per-staff bound.
    """
    band = get_band(shift, role)
    return band["min"] if band else 0


# ---------- legacy compatibility re-exports ----------
#
# Other modules (solver, postsolve, tests) reach into these dicts directly.
# We expose them as live views over the active values so callers don't
# need to update — but new code should prefer the helper functions above.

def _build_legacy_view(group: str) -> dict[tuple[Role, str], int]:
    """``group`` is one of ``"min"`` / ``"target"`` / ``"max"``. Returns
    a ``(Role, "day"|"night")`` dict where "day" merges M/A/M1/A1 (we
    use M's values as the representative — they're the same in defaults)
    and "night" maps from N."""
    def day_for(role: Role, key: str) -> int:
        band = get_band("M", role)
        return band[key] if band else 0
    def night_for(role: Role, key: str) -> int:
        band = get_band("N", role)
        return band[key] if band else 0
    return {
        (Role.STAFF, "day"):   day_for(Role.STAFF, group),
        (Role.STAFF, "night"): night_for(Role.STAFF, group),
        (Role.ZC,    "day"):   day_for(Role.ZC, group),
        (Role.ZC,    "night"): night_for(Role.ZC, group),
    }


def __getattr__(name: str):  # pragma: no cover — module-level dynamic attr
    """Module-level ``__getattr__`` so the legacy names
    ``H16_HARD_CAPS`` / ``S1_PREFERRED_TARGETS`` / ``S1_ACCEPTABLE_MAX``
    still resolve to live dicts. Existing code that did ``from .caps
    import H16_HARD_CAPS`` keeps working without each call site having
    to migrate to the new helper functions in one pass."""
    if name == "H16_HARD_CAPS":
        return _build_legacy_view("max")
    if name == "S1_PREFERRED_TARGETS":
        return _build_legacy_view("min")
    if name == "S1_ACCEPTABLE_MAX":
        return _build_legacy_view("target")
    raise AttributeError(name)


__all__ = [
    "acceptable_max_for",
    "aggregate_capacity",
    "get_band",
    "hard_cap_for",
    "preferred_target_for",
    "preferred_target_for_shift_role",
    "reload_defaults",
    "reset_overrides",
    "set_iteration_band",
    "set_iteration_per_staff_cap",
    "set_iteration_per_staff_preferred",
    "snapshot_iteration_overrides",
]
