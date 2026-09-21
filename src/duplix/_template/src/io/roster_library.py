"""Read the stored period rosters as one merged roster.

The staff roster arrives about once a month and the periods don't line
up with calendar months (31 Aug - 27 Sep, then 28 Sep - 25 Oct, ...).
The app keeps every roster it has been given; this module turns "the
rosters on file" into the rows for one allocation date:

  * pick the stored files that have a column for the date (and the day
    after it — Plan reads D and D+1);
  * read each with the normal wide-roster reader;
  * merge people across files by employee id, dated cells from a later
    upload overriding the same date from an earlier one.

So on the last day of a roster, D comes from the current file and D+1
comes from the next one, without the operator doing anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date as date_t
from typing import TypeVar

from ..config import Config
from ..schemas import AMRosterRow, CrewRosterRow
from ..state import AppState
from .readers import read_am_roster, read_staff_roster, workbook_from_bytes

Row = TypeVar("Row", CrewRosterRow, AMRosterRow)


def read_roster_bytes(kind: str, data: bytes, config: Config) -> list:
    """Parse one uploaded roster of ``kind`` into typed rows."""
    reader = {"staff_roster": read_staff_roster, "am_roster": read_am_roster}[kind]
    wb = workbook_from_bytes(data)
    try:
        return reader(wb, config)
    finally:
        wb.close()


def roster_dates(kind: str, data: bytes, config: Config) -> set[date_t]:
    """Every date the workbook has a column for. Also validates the file
    (missing ID column, duplicate ids, unknown layout) — the upload
    endpoint calls this so a bad roster is rejected at the door with the
    reader's own message instead of failing later inside Plan."""
    dates: set[date_t] = set()
    for row in read_roster_bytes(kind, data, config):
        dates.update(row.status_by_date.keys())
    return dates


def merge_rows(per_file_rows: Sequence[Sequence[Row]]) -> list[Row]:
    """Merge rows from several rosters, oldest upload first.

    Same employee id -> one row: identity fields (name, licence, role)
    come from the newest file that lists the person, and per-date cells
    are unioned with the newest file winning any date both cover.
    """
    merged: dict[str, Row] = {}
    for rows in per_file_rows:
        for row in rows:
            prev = merged.get(row.employee_id)
            if prev is None:
                merged[row.employee_id] = row
                continue
            merged[row.employee_id] = row.model_copy(update={
                "status_by_date": {**prev.status_by_date, **row.status_by_date},
                "raw_status_by_date": {
                    **getattr(prev, "raw_status_by_date", {}),
                    **getattr(row, "raw_status_by_date", {}),
                },
            })
    return list(merged.values())


def read_library(
    state: AppState, kind: str, config: Config,
    window: Iterable[date_t] | None = None,
) -> list:
    """Rows for ``kind`` from the stored rosters relevant to ``window``.

    Raises FileNotFoundError (with the UI's own wording) when nothing has
    been uploaded for ``kind``.
    """
    files = state.roster_files_for(kind, window)
    if not files:
        state.input_bytes(kind)          # raises the standard message
    return merge_rows([read_roster_bytes(kind, f.data, config) for f in files])
