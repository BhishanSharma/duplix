"""In-memory application state — the single store the whole app runs on.

There is no workbook, no database and no per-date file tree. The three
input files arrive as uploads from the browser and are held as bytes;
every engine stage reads its inputs from this object and writes its
outputs back into it; the web layer serialises it to JSON for the
dashboard and to a one-off .xlsx for the Download button.

Lifetime is the server process. Restarting the server clears everything
and the operator re-uploads the day's three files.

Threading: ``ThreadingHTTPServer`` means several requests can touch the
state at once, so every mutation goes through ``STATE.lock``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date as date_t
from datetime import datetime
from typing import Any

from .schemas import (
    AllocationRow,
    AvailabilityRow,
    CleanFlightRow,
    OpsClass,
    Pair,
    WarningRow,
    WorkloadSummaryRow,
)

# The three files the operator uploads. Keys are the API's ``kind``
# path segment; the labels are what the UI shows next to each slot.
INPUT_KINDS: tuple[str, ...] = ("sv_portal", "staff_roster", "am_roster")
INPUT_LABELS: dict[str, str] = {
    "sv_portal": "Flight schedule (SV portal export)",
    "staff_roster": "Staff roster",
    "am_roster": "AM List",
}

# How often each file actually changes, which is what decides where the
# UI puts it. The flight schedule is a fresh export every morning, so it
# stays on the dashboard where the day's work starts. The two rosters are
# published for a whole period — uploaded once and then left alone — so
# they live in the Setup sidebar, out of the daily path.
DAILY_INPUT_KINDS: tuple[str, ...] = ("sv_portal",)
SETUP_INPUT_KINDS: tuple[str, ...] = ("staff_roster", "am_roster")
INPUT_CADENCE: dict[str, str] = {
    **{k: "daily" for k in DAILY_INPUT_KINDS},
    **{k: "setup" for k in SETUP_INPUT_KINDS},
}

# Where the operator fixes a given file, quoted back in the
# "not uploaded yet" error so the message names a real place in the UI.
INPUT_HOMES: dict[str, str] = {
    "daily": "the Flight schedule card on the dashboard",
    "setup": "the Setup sidebar (Setup button, top right)",
}

# Column set for an override row. Previously the IN_Override sheet's
# header; now just the keys each override dict may carry. Order is the
# column order the drawer renders.
OVERRIDE_HEADERS: tuple[str, ...] = (
    "type", "employee", "shift", "limit",
    "flight", "std", "other_std",
    "flt", "dep", "arr", "ops_class", "date",
    "ac_type", "ac", "pax", "owner", "role",
    "from_time", "to_time",
)

# What the drawer renders as editable columns. The rest stay in the row
# payload (so a Phase-R row keeps its flight/std) but are not worth a
# column each in a hand-edited table.
OVERRIDE_HEADERS_VISIBLE: tuple[str, ...] = (
    "type", "employee", "shift", "limit",
)


@dataclass
class UploadedInput:
    """One uploaded .xlsx, held in memory."""

    kind: str
    filename: str
    data: bytes
    uploaded_at: datetime = field(default_factory=datetime.now)

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": INPUT_LABELS.get(self.kind, self.kind),
            "cadence": INPUT_CADENCE.get(self.kind, "daily"),
            "filename": self.filename,
            "size_kb": round(len(self.data) / 1024, 1),
            "uploaded_at": self.uploaded_at.isoformat(timespec="seconds"),
        }


@dataclass
class AppState:
    """Everything the app knows, for one operator, for one day."""

    lock: threading.RLock = field(default_factory=threading.RLock)

    # ---- inputs (uploaded from the dashboard and the Setup sidebar) ----
    inputs: dict[str, UploadedInput] = field(default_factory=dict)
    run_date: date_t | None = None

    # ---- overrides (edited via the drawer) ----
    overrides: list[dict[str, str]] = field(default_factory=list)

    # ---- Plan stage outputs ----
    cleaned: dict[OpsClass, list[CleanFlightRow]] = field(default_factory=dict)
    availability: list[AvailabilityRow] = field(default_factory=list)
    plan_summary: dict[str, Any] | None = None
    plan_text: str = ""
    staffing: list[dict[str, Any]] = field(default_factory=list)

    # ---- Allocate stage outputs ----
    allocations: list[AllocationRow] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)
    workload: list[WorkloadSummaryRow] = field(default_factory=list)
    warnings: list[WarningRow] = field(default_factory=list)
    unallocated: list[dict[str, Any]] = field(default_factory=list)
    recommendations: dict[str, Any] = field(default_factory=dict)

    # ---- run status ----
    last_run: str = ""
    mode: str = ""
    solver_status: str = ""
    duration_s: float | None = None

    # Snapshot of the previous solve's {flight_key: staff_name}. Marks
    # redistributed rows in the Allocations tab and warm-starts the
    # next solve.
    prev_staff_by_key: dict[str, str] = field(default_factory=dict)

    # ---------------- inputs ----------------

    def set_input(self, kind: str, filename: str, data: bytes) -> UploadedInput:
        if kind not in INPUT_KINDS:
            raise ValueError(f"unknown input kind {kind!r}")
        with self.lock:
            up = UploadedInput(kind=kind, filename=filename, data=data)
            self.inputs[kind] = up
            return up

    def clear_input(self, kind: str) -> None:
        with self.lock:
            self.inputs.pop(kind, None)

    def input_bytes(self, kind: str) -> bytes:
        up = self.inputs.get(kind)
        if up is None:
            home = INPUT_HOMES.get(INPUT_CADENCE.get(kind, "daily"), "the dashboard")
            raise FileNotFoundError(
                f"{INPUT_LABELS.get(kind, kind)} has not been uploaded yet — "
                f"upload it from {home}."
            )
        return up.data

    def missing_inputs(self, cadence: str | None = None) -> list[str]:
        """Kinds with no file yet. ``cadence`` narrows it to one group, so
        the UI can tell "no schedule today" apart from "rosters were
        never set up" — two different fixes, in two different places."""
        return [
            k for k in INPUT_KINDS
            if k not in self.inputs
            and (cadence is None or INPUT_CADENCE.get(k) == cadence)
        ]

    def inputs_as_json(self) -> list[dict[str, Any]]:
        out = []
        for kind in INPUT_KINDS:
            up = self.inputs.get(kind)
            if up is None:
                out.append({
                    "kind": kind,
                    "label": INPUT_LABELS[kind],
                    "cadence": INPUT_CADENCE.get(kind, "daily"),
                    "filename": "",
                    "size_kb": 0,
                    "uploaded_at": "",
                })
            else:
                out.append(up.as_json())
        return out

    # ---------------- overrides ----------------

    def add_override(self, row: dict[str, str]) -> int:
        """Append an override row. Returns its 0-based index."""
        with self.lock:
            self.overrides.append(_clean_override(row))
            return len(self.overrides) - 1

    def update_override(self, index: int, row: dict[str, str]) -> None:
        with self.lock:
            if not 0 <= index < len(self.overrides):
                raise IndexError(f"no override row at index {index}")
            self.overrides[index] = _clean_override(row)

    def delete_override(self, index: int) -> None:
        with self.lock:
            if not 0 <= index < len(self.overrides):
                raise IndexError(f"no override row at index {index}")
            del self.overrides[index]

    # ---------------- lifecycle ----------------

    def reset_results(self) -> dict[str, int]:
        """Clear every engine output but keep the uploads and the
        override rows. This is what the Reset button does."""
        with self.lock:
            cleared = {
                "cleaned": sum(len(v) for v in self.cleaned.values()),
                "availability": len(self.availability),
                "allocations": len(self.allocations),
                "pairs": len(self.pairs),
                "workload": len(self.workload),
                "warnings": len(self.warnings),
                "unallocated": len(self.unallocated),
            }
            self.cleaned = {}
            self.availability = []
            self.plan_summary = None
            self.plan_text = ""
            self.staffing = []
            self.allocations = []
            self.pairs = []
            self.workload = []
            self.warnings = []
            self.unallocated = []
            self.recommendations = {}
            self.prev_staff_by_key = {}
            self.last_run = ""
            self.mode = ""
            self.solver_status = ""
            self.duration_s = None
            return cleared

    def stamp_run(
        self, *, mode: str, solver_status: str, duration_s: float | None,
    ) -> None:
        with self.lock:
            self.last_run = datetime.now().isoformat(timespec="seconds")
            self.mode = mode
            self.solver_status = solver_status
            self.duration_s = duration_s

    # ---------------- convenience views ----------------

    def all_cleaned(self) -> list[CleanFlightRow]:
        out: list[CleanFlightRow] = []
        for rows in self.cleaned.values():
            out.extend(rows)
        return out

    def cleaned_for(self, *ops: OpsClass) -> list[CleanFlightRow]:
        out: list[CleanFlightRow] = []
        for o in ops:
            out.extend(self.cleaned.get(o, []))
        return out

    def allocatable_cleaned(self) -> list[CleanFlightRow]:
        """Everything the solver is allowed to assign — every cleaned
        flight except GULF, which is extract-only."""
        return [
            r for r in self.all_cleaned() if r.ops_class is not OpsClass.GULF
        ]


def _clean_override(row: dict[str, str]) -> dict[str, str]:
    """Normalise an override row: lowercased keys, stripped string
    values, blanks dropped."""
    out: dict[str, str] = {}
    for k, v in row.items():
        key = str(k).strip().lower()
        if not key:
            continue
        s = "" if v is None else str(v).strip()
        if s:
            out[key] = s
    return out


# The process-wide singleton. Imported directly by the engine stages and
# the web layer — there is exactly one operator per server.
STATE = AppState()
