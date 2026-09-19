"""Runs Plan / Allocate on a background thread for the web UI.

State lives in this process, so the stages run in-thread rather than in
a subprocess — there is no workbook to hand off through. The UI fires
POST /api/run and then polls GET /api/run/status until it settles.

Run state is module-level: the server is single-process and serves one
assigner.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import date as date_t
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..state import AppState

RunState = Literal["idle", "running", "ok", "failed"]

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "config.yml"


@dataclass
class _Run:
    state: RunState = "idle"
    step: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False
    """Set by Reset while a run is in flight. The worker checks it before
    committing results, so a cancelled run leaves the freshly-cleared
    state alone instead of repopulating it."""


_run = _Run()
_lock = threading.Lock()


def status() -> dict[str, object]:
    """Snapshot for ``GET /api/run/status``."""
    with _lock:
        elapsed = None
        if _run.started_at:
            end = _run.finished_at or datetime.now()
            elapsed = round((end - _run.started_at).total_seconds(), 1)
        return {
            "state": _run.state,
            "step": _run.step,
            "started_at": _run.started_at.isoformat(timespec="seconds")
            if _run.started_at else None,
            "finished_at": _run.finished_at.isoformat(timespec="seconds")
            if _run.finished_at else None,
            "elapsed_s": elapsed,
            "error": _run.error,
            "counts": dict(_run.counts),
        }


def is_running() -> bool:
    with _lock:
        return _run.state == "running"


def cancel() -> bool:
    """Ask an in-flight run to discard its results. Returns True when a
    run was actually in flight.

    The solver runs under its own ``max_seconds`` ceiling, so the thread
    finishes on its own shortly; marking it cancelled means whatever it
    produces is thrown away rather than written over the state the
    operator just reset.
    """
    with _lock:
        if _run.state != "running":
            return False
        _run.cancelled = True
        return True


def trigger(state: AppState, d_day: date_t, sub_command: str) -> bool:
    """Start a run on a background thread. Returns False when one is
    already in flight."""
    with _lock:
        if _run.state == "running":
            return False
        _run.state = "running"
        _run.step = sub_command
        _run.started_at = datetime.now()
        _run.finished_at = None
        _run.error = ""
        _run.counts = {}
        _run.cancelled = False

    thread = threading.Thread(
        target=_worker, args=(state, d_day, sub_command),
        name=f"flight-alloc-{sub_command}", daemon=True,
    )
    thread.start()
    return True


def _worker(state: AppState, d_day: date_t, sub_command: str) -> None:
    counts: dict[str, int] = {}
    error = ""
    try:
        counts = _dispatch(state, d_day, sub_command)
    except FileNotFoundError as exc:
        # A missing upload is an operator problem, not a crash — say so
        # plainly rather than dumping a traceback into the UI.
        error = str(exc)
        print(f"[run] {sub_command} blocked: {error}")
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    with _lock:
        cancelled = _run.cancelled
        _run.finished_at = datetime.now()
        _run.error = "" if cancelled else error
        _run.counts = {} if cancelled else counts
        _run.state = "ok" if (cancelled or not error) else "failed"
        _run.cancelled = False

    if cancelled:
        # Results are discarded; the state the operator reset stands.
        state.reset_results()
        print(f"[run] {sub_command} finished but was cancelled — results dropped")


def _dispatch(state: AppState, d_day: date_t, sub_command: str) -> dict[str, int]:
    """Run one stage and return its counts."""
    from ..plan import run as run_plan
    from ..step1_clean_flights import run as run_step1
    from ..step2_extract_roster import run as run_step2
    from ..step3_allocate_flights import run as run_allocate

    if sub_command == "plan":
        run_plan(state, d_day, CONFIG_PATH)
        return {"FLIGHTS": len(state.allocatable_cleaned()),
                "STAFF": len(state.availability)}

    if sub_command == "step1":
        return {k.value: v for k, v in run_step1(state, d_day, CONFIG_PATH).items()}

    if sub_command == "step2":
        return run_step2(state, d_day, CONFIG_PATH)

    if sub_command in ("step3", "allocate"):
        return run_allocate(state, d_day, CONFIG_PATH, mode_label="Allocate")

    if sub_command == "all":
        # Plan first so cleaned flights + availability are rebuilt from
        # the uploads, then solve on top of them.
        run_plan(state, d_day, CONFIG_PATH)
        return run_allocate(
            state, d_day, CONFIG_PATH,
            append_warnings=True, mode_label="Allocate",
        )

    raise ValueError(f"unknown run step: {sub_command!r}")
