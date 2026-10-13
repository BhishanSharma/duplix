"""Session-scoped readbacks: the uploaded input files, the override
rows, the SV-portal column probe and the per-shift roster counts.

"Session" because all of it lives in the server's memory for as long as
the process runs — nothing here is derived from a solve.
"""

from __future__ import annotations

from collections import Counter
from datetime import date as date_t
from datetime import datetime
from typing import Any

from ...config import load_config
from ...state import (
    DAILY_INPUT_KINDS,
    OVERRIDE_HEADERS,
    OVERRIDE_HEADERS_VISIBLE,
    SETUP_INPUT_KINDS,
    AppState,
)
from .common import CONFIG_PATH, SHIFTS, is_live, run_date_iso



def read_inputs(state: AppState) -> dict[str, Any]:
    """Upload slots: what's loaded, what's missing, split by cadence.

    ``daily`` is the flight schedule the dashboard asks for every
    morning; ``setup`` is the two rosters the Setup sidebar owns. The UI
    reports each group separately because they're fixed in different
    places, and ``ready`` still means "all three are in".
    """
    with state.lock:
        missing = state.missing_inputs()
        missing_daily = state.missing_inputs("daily")
        missing_setup = state.missing_inputs("setup")
        return {
            "inputs": state.inputs_as_json(),
            "missing": missing,
            "ready": not missing,
            "groups": {
                "daily": {
                    "kinds": list(DAILY_INPUT_KINDS),
                    "missing": missing_daily,
                    "ready": not missing_daily,
                },
                "setup": {
                    "kinds": list(SETUP_INPUT_KINDS),
                    "missing": missing_setup,
                    "ready": not missing_setup,
                },
            },
            "run_date": run_date_iso(state),
        }


def read_server_date(state: AppState) -> dict[str, Any]:
    """The operating date, decided by the server rather than the browser.

    The console is driven from whatever machine is free, and a laptop
    with a stale clock or a different timezone used to silently plan the
    wrong day. ``date`` is the server's today; ``run_date`` is the date
    the current session is already working on, which wins when set so a
    reload doesn't throw away a day the operator deliberately chose.
    """
    today = date_t.today()
    with state.lock:
        run_date = run_date_iso(state)
    return {
        "date": today.isoformat(),
        "run_date": run_date,
        # What the date input should show: the session's date if there is
        # one, else the server's today.
        "effective": run_date or today.isoformat(),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


def read_sv_portal_columns(state: AppState) -> dict[str, Any]:
    """Header list plus a sample of distinct values per column from the
    uploaded flight schedule. Feeds the drawer's extraction-filter form,
    where the custom-header field becomes a dropdown of real headers
    with a hint of what's in them.

    Sample capped at 20 distinct values per column and 5000 scanned rows
    to keep the payload light. Empty lists on any failure — the form
    degrades to a free-text input.
    """
    from ...io.readers import workbook_from_bytes

    try:
        data = state.input_bytes("sv_portal")
    except FileNotFoundError:
        return {"headers": [], "samples": {}}
    try:
        wb = workbook_from_bytes(data)
    except Exception as exc:  # noqa: BLE001 — degrade to free-text input
        print(f"  sv_portal headers unreadable: {type(exc).__name__}: {exc}")
        return {"headers": [], "samples": {}}
    from ...io.readers import SHEET_IN_SVPORTAL, find_sv_header, pick_sheet

    try:
        if not wb.sheetnames:
            return {"headers": [], "samples": {}}
        ws = pick_sheet(wb, SHEET_IN_SVPORTAL)
        top = [list(r) for _, r in zip(range(16), ws.iter_rows(values_only=True))]
        if not top:
            return {"headers": [], "samples": {}}
        # Same header row the flight reader uses (it may sit below a
        # title banner). Keep each header's own column index so a blank
        # header cell does not shift every later column.
        cfg = load_config(CONFIG_PATH).io.sv_portal
        header_idx, _ = find_sv_header(top, cfg.columns)
        cols = [
            (i, str(h).strip()) for i, h in enumerate(top[header_idx])
            if h is not None and str(h).strip()
        ]
        headers = [h for _, h in cols]
        samples: dict[str, list[str]] = {h: [] for h in headers}
        seen: dict[str, set[str]] = {h: set() for h in headers}
        for n, row in enumerate(
            ws.iter_rows(min_row=header_idx + 2, values_only=True),
        ):
            if n >= 5000:
                break
            for i, header in cols:
                if i >= len(row) or row[i] is None:
                    continue
                v = str(row[i]).strip()
                if not v or v in seen[header] or len(samples[header]) >= 20:
                    continue
                seen[header].add(v)
                samples[header].append(v)
        return {"headers": headers, "samples": samples}
    finally:
        wb.close()


def read_overrides(state: AppState) -> dict[str, Any]:
    """Override rows for the drawer table. ``headers`` are the editable
    columns; each row also carries its full payload so a type-specific
    field (a Phase-R ``flight`` / ``std``) survives an edit."""
    with state.lock:
        rows = [
            {
                "index": i,
                "values": [row.get(h, "") for h in OVERRIDE_HEADERS_VISIBLE],
                "payload": dict(row),
            }
            for i, row in enumerate(state.overrides)
        ]
    return {
        "headers": list(OVERRIDE_HEADERS_VISIBLE),
        "all_headers": list(OVERRIDE_HEADERS),
        "rows": rows,
    }


def read_roster_counts(state: AppState) -> dict[str, Any]:
    """Per-shift staff counts — small helper for the dashboard header."""
    with state.lock:
        counts: Counter[str] = Counter()
        for av in state.availability:
            if is_live(state, av):
                counts[av.current_shift] += 1
    return {"counts": {s: counts.get(s, 0) for s in SHIFTS}}

