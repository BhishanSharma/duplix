"""In-memory application state — the single store the whole app runs on.

There is no workbook, no database and no per-date file tree. The three
input files arrive as uploads from the browser and are held as bytes;
every engine stage reads its inputs from this object and writes its
outputs back into it; the web layer serialises it to JSON for the
dashboard and to a one-off .xlsx for the Download button.

Lifetime is the server process. Restarting the server clears the
results, the overrides and the daily flight schedule. The two roster
files are the exception: they cover a whole period, so every upload is
also kept on disk (``roster_store``) and reloaded at start-up, and
several rosters can be held at once — the one that covers the
allocation date is the one that gets used.

Threading: ``ThreadingHTTPServer`` means several requests can touch the
state at once, so every mutation goes through ``STATE.lock``.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, field
from collections.abc import Iterable
from datetime import date as date_t
from datetime import datetime, timedelta
from typing import Any

from . import roster_store
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
class RosterFile:
    """One uploaded period roster. Unlike the daily schedule, several of
    these can be held at once (Aug-Sep, then Sep-Oct, ...); ``dates`` is
    every calendar date the workbook has a column for."""

    id: str
    kind: str
    filename: str
    data: bytes
    dates: list[date_t]
    uploaded_at: datetime = field(default_factory=datetime.now)

    @property
    def start(self) -> date_t | None:
        return min(self.dates) if self.dates else None

    @property
    def end(self) -> date_t | None:
        return max(self.dates) if self.dates else None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "size_kb": round(len(self.data) / 1024, 1),
            "uploaded_at": self.uploaded_at.isoformat(timespec="seconds"),
            "start": self.start.isoformat() if self.start else "",
            "end": self.end.isoformat() if self.end else "",
            "days": len(self.dates),
        }


@dataclass
class AppState:
    """Everything the app knows, for one operator, for one day."""

    lock: threading.RLock = field(default_factory=threading.RLock)

    # ---- inputs (uploaded from the dashboard and the Setup sidebar) ----
    inputs: dict[str, UploadedInput] = field(default_factory=dict)
    # Period rosters (SETUP_INPUT_KINDS), oldest upload first. Kept apart
    # from ``inputs`` because there can be several per kind.
    rosters: dict[str, list[RosterFile]] = field(default_factory=dict)
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
        """Store a single-file input (the daily schedule). Rosters go
        through ``add_roster`` because they accumulate."""
        if kind not in INPUT_KINDS:
            raise ValueError(f"unknown input kind {kind!r}")
        if kind in SETUP_INPUT_KINDS:
            raise ValueError(f"{kind!r} is a period roster; use add_roster()")
        with self.lock:
            up = UploadedInput(kind=kind, filename=filename, data=data)
            self.inputs[kind] = up
            return up

    def clear_input(self, kind: str) -> None:
        """Drop an input. For a roster kind that means every stored roster
        of that kind, from memory and from disk."""
        with self.lock:
            if kind in SETUP_INPUT_KINDS:
                for rf in self.rosters.pop(kind, []):
                    roster_store.delete(rf.id)
            else:
                self.inputs.pop(kind, None)

    def input_bytes(self, kind: str) -> bytes:
        if kind in SETUP_INPUT_KINDS:
            files = self.rosters.get(kind) or []
            if not files:
                raise FileNotFoundError(self._missing_message(kind))
            return files[-1].data
        up = self.inputs.get(kind)
        if up is None:
            raise FileNotFoundError(self._missing_message(kind))
        return up.data

    @staticmethod
    def _missing_message(kind: str) -> str:
        home = INPUT_HOMES.get(INPUT_CADENCE.get(kind, "daily"), "the dashboard")
        return (
            f"{INPUT_LABELS.get(kind, kind)} has not been uploaded yet — "
            f"upload it from {home}."
        )

    def _has_input(self, kind: str) -> bool:
        if kind in SETUP_INPUT_KINDS:
            return bool(self.rosters.get(kind))
        return kind in self.inputs

    def missing_inputs(self, cadence: str | None = None) -> list[str]:
        """Kinds with no file yet. ``cadence`` narrows it to one group, so
        the UI can tell "no schedule today" apart from "rosters were
        never set up" — two different fixes, in two different places."""
        return [
            k for k in INPUT_KINDS
            if not self._has_input(k)
            and (cadence is None or INPUT_CADENCE.get(k) == cadence)
        ]

    def inputs_as_json(self) -> list[dict[str, Any]]:
        out = []
        for kind in INPUT_KINDS:
            base = {
                "kind": kind,
                "label": INPUT_LABELS[kind],
                "cadence": INPUT_CADENCE.get(kind, "daily"),
            }
            if kind in SETUP_INPUT_KINDS:
                files = self.rosters.get(kind) or []
                newest = files[-1] if files else None
                out.append({
                    **base,
                    "filename": newest.filename if newest else "",
                    "size_kb": round(len(newest.data) / 1024, 1) if newest else 0,
                    "uploaded_at": (
                        newest.uploaded_at.isoformat(timespec="seconds")
                        if newest else ""
                    ),
                    # Newest first: the one on top is the one that wins
                    # wherever two rosters overlap.
                    "files": [f.as_json() for f in reversed(files)],
                })
                continue
            up = self.inputs.get(kind)
            if up is None:
                out.append({**base, "filename": "", "size_kb": 0, "uploaded_at": ""})
            else:
                out.append(up.as_json())
        return out

    # ---------------- period rosters ----------------

    def add_roster(
        self, kind: str, filename: str, data: bytes, dates: Iterable[date_t],
        *, persist: bool = True,
    ) -> RosterFile:
        """Keep an uploaded roster alongside the ones already held.

        Re-uploading the same file (same name or same bytes) replaces
        the earlier copy rather than piling up duplicates. A roster for
        a different period is added; the rosters stay ordered by upload
        time, and where two overlap the later upload wins.
        """
        if kind not in SETUP_INPUT_KINDS:
            raise ValueError(f"{kind!r} is not a period roster")
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            kept: list[RosterFile] = []
            for rf in self.rosters.get(kind, []):
                same = (
                    rf.filename == filename
                    or hashlib.sha256(rf.data).hexdigest() == digest
                )
                if same:
                    roster_store.delete(rf.id)
                else:
                    kept.append(rf)
            now = datetime.now()
            rf = RosterFile(
                id=f"{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}",
                kind=kind, filename=filename, data=data,
                dates=sorted(set(dates)), uploaded_at=now,
            )
            kept.append(rf)
            self.rosters[kind] = kept
            if persist:
                roster_store.save(
                    file_id=rf.id, kind=kind, filename=filename,
                    uploaded_at=now, dates=rf.dates, data=data,
                )
            return rf

    def remove_roster(self, kind: str, file_id: str) -> bool:
        with self.lock:
            files = self.rosters.get(kind, [])
            keep = [f for f in files if f.id != file_id]
            if len(keep) == len(files):
                return False
            self.rosters[kind] = keep
            roster_store.delete(file_id)
            return True

    def load_persisted_rosters(self) -> int:
        """Reload the rosters saved by earlier sessions. Called once at
        server start; returns how many came back."""
        loaded = 0
        with self.lock:
            for stored in roster_store.load_all():
                if stored.kind not in SETUP_INPUT_KINDS:
                    continue
                self.rosters.setdefault(stored.kind, []).append(RosterFile(
                    id=stored.id, kind=stored.kind, filename=stored.filename,
                    data=stored.data, dates=sorted(stored.dates),
                    uploaded_at=stored.uploaded_at,
                ))
                loaded += 1
            for files in self.rosters.values():
                files.sort(key=lambda f: f.uploaded_at)
        return loaded

    def roster_files_for(
        self, kind: str, window: Iterable[date_t] | None = None,
    ) -> list[RosterFile]:
        """The stored rosters that matter for ``window`` (oldest upload
        first, so a later upload overrides an earlier one on the merge).

        Only rosters with a column in the window are returned. When none
        has, every stored roster is returned instead: the reader then
        loads real rows, finds no cell for the date, and the coverage
        warning (W010 / W011) names the missing date — better than
        silently planning with an empty roster.
        """
        with self.lock:
            files = list(self.rosters.get(kind, []))
        if not files or window is None:
            return files
        wanted = set(window)
        hits = [f for f in files if wanted.intersection(f.dates)]
        return hits or files

    def roster_coverage(self, day: date_t) -> dict[str, Any]:
        """Does each roster kind have a column for ``day`` and ``day+1``
        (the two days the Plan stage reads)? Drives the notice in the
        Setup sidebar so a stale roster is caught before Plan runs."""
        next_day = day + timedelta(days=1)
        out: dict[str, Any] = {"date": day.isoformat(), "notices": []}
        with self.lock:
            for kind in SETUP_INPUT_KINDS:
                files = self.rosters.get(kind, [])
                covered = {d for f in files for d in f.dates}
                info = {
                    "loaded": bool(files),
                    "covers_date": day in covered,
                    "covers_next_day": next_day in covered,
                }
                out[kind] = info
                label = INPUT_LABELS[kind]
                if not files:
                    continue
                if not info["covers_date"]:
                    out["notices"].append(
                        f"{label}: no roster covers {day:%d %b %Y}. "
                        "Upload the roster for that period."
                    )
                elif not info["covers_next_day"]:
                    out["notices"].append(
                        f"{label}: the roster ends on {day:%d %b %Y}. "
                        "Upload the next one before planning the following day."
                    )
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
