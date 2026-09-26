"""Build a downloadable .xml of just the combined Allocations rows.

Companion to export.py's build_workbook(): that renders the *whole*
state (Summary, Roster, Warnings, ...) as a multi-sheet .xlsx. This
renders only the thing most people actually read off after a run —
flight -> staff, across all three allocation lanes (DayOps, NightOps,
P2F) — as a single flat XML file. Nothing here is ever read back in.
"""

from __future__ import annotations

import io
from xml.dom import minidom
from xml.etree.ElementTree import Element, ElementTree, SubElement

from ..schemas import AllocationSheet
from ..state import AppState

_SHEET_LABEL = {
    AllocationSheet.DAY_OPS: "DayOps",
    AllocationSheet.NIGHT_OPS: "NightOps",
    AllocationSheet.P2F: "P2F",
}


def build_allocations_xml(state: AppState) -> bytes:
    """Render every allocation row (all three sheets, combined) as XML.

    One <allocation> element per flight, each carrying the same fields
    as an Allocations_* row in the .xlsx (flight, staff, and the
    handover/warning context), plus a ``sheet`` attribute so a reader
    can tell DayOps/NightOps/P2F rows apart without three files.
    """
    with state.lock:
        run_date = state.run_date.isoformat() if state.run_date else ""
        root = Element("allocations", {"date": run_date})
        for r in state.allocations:
            SubElement(root, "allocation", {
                "sheet": _SHEET_LABEL.get(r.sheet_target, ""),
                "date": r.date.isoformat(),
                "flt": r.flt,
                "dep": r.dep,
                "arr": r.arr,
                "std": r.std.isoformat(timespec="minutes"),
                "pax": str(r.pax),
                "staff": r.staff_name,
                "planned_by": r.planned_by_name or "",
                "relieved_by": r.relieved_by_name or "",
                "warning": r.warning or "",
            })

    buf = io.BytesIO()
    ElementTree(root).write(buf, encoding="utf-8", xml_declaration=True)
    # Re-serialize with indentation — this is meant to be opened
    # directly by a human, not just piped into another program.
    pretty = minidom.parseString(buf.getvalue()).toprettyxml(indent="  ")
    return pretty.encode("utf-8")


def filename_for_xml(state: AppState) -> str:
    """A download name that carries the allocation date."""
    d = state.run_date.isoformat() if state.run_date else "undated"
    return f"flight_allocation_{d}_allocations.xml"


__all__ = ["build_allocations_xml", "filename_for_xml"]
