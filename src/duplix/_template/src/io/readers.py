"""Excel readers for the three uploaded input files.

The operator uploads the day's three workbooks from the dashboard; the
bytes live in ``state.STATE.inputs``. ``workbook_from_bytes`` turns a
payload into an openpyxl handle and the readers below pull typed rows
out of it.

Sheet lookup is forgiving: each reader asks for its canonical sheet
name (``IN_SVportal`` / ``IN_Staff`` / ``IN_AM_Roster``) and falls back
to the workbook's first sheet when that name is absent, because a file
exported straight out of the SV portal or the rostering tool carries
whatever sheet name that tool chose.

Override rows are no longer read from a worksheet at all — they live in
memory and are parsed by the ``read_*`` functions in the second half of
this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from ..config import Config, StatusConfig
from ..schemas import (
    AMRosterRow,
    CrewRosterRow,
    CrewStatus,
    P2FNomination,
    PerStaffOverride,
    RaiseCapForFlight,
    RawFlightRow,
    Role,
    ShiftCode,
    SkipINTLRemoval,
    SkipP2FBuffer,
    WaiveH10Pair,
)

# Canonical sheet names, tried first in every uploaded workbook.
SHEET_IN_SVPORTAL = "IN_SVportal"
SHEET_IN_STAFF = "IN_Staff"
SHEET_IN_AM_ROSTER = "IN_AM_Roster"


def workbook_from_bytes(data: bytes) -> Workbook:
    """Open an uploaded payload as a workbook. Accepts .xlsx/.xlsm, old
    binary .xls, HTML-table "xls" exports and CSV/TSV text — see
    ``upload_formats``."""
    from .upload_formats import to_workbook
    return to_workbook(data)


def norm_header(value: Any) -> str:
    """A header label reduced to what matters: lower case, punctuation
    and repeated spaces dropped. 'Flight No.', 'FLIGHT_NO' and
    ' flight  no ' all become 'flight no' — so a minor relabel in an
    export does not lose the column."""
    if value is None:
        return ""
    s = str(value).strip().lower()
    s = re.sub(r"[._\-/:()\[\]#,]+", " ", s)
    return " ".join(s.split())


def pick_sheet(wb: Workbook, preferred: str) -> Worksheet:
    """Return ``wb[preferred]`` when present, else the first sheet.

    Uploaded files come straight from email, so we cannot insist on a
    sheet name. Raises ValueError only when the workbook has no sheets
    at all.
    """
    if preferred in wb.sheetnames:
        return wb[preferred]
    if not wb.sheetnames:
        raise ValueError("uploaded workbook has no sheets")
    return wb[wb.sheetnames[0]]


# ---------- shared parsing helpers ----------

_DATE_FORMAT_FALLBACKS: tuple[str, ...] = (
    # ISO + common slash variants
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",          # US: 4/25/2026
    "%d/%m/%Y",          # UK / IN: 25/4/2026
    "%m-%d-%Y",
    "%d-%m-%Y",
    # Compact
    "%Y%m%d",
    # With month name
    "%d %b %Y",          # 25 Apr 2026
    "%d-%b-%Y",          # 25-Apr-2026
    "%d/%b/%Y",          # 25/Apr/2026
    "%d-%b-%y",          # 25-Apr-26
    "%b %d, %Y",         # Apr 25, 2026
    "%d %B %Y",          # 25 April 2026
    "%d-%B-%Y",
    # Two-digit year
    "%m/%d/%y",
    "%d/%m/%y",
    "%y-%m-%d",
)


def _parse_date(v: Any, fmt: str) -> date_type | None:
    """Parse a cell value to a date. The configured ``fmt`` is tried
    first, then a wide set of common variants — per user direction
    2026-05-10, real exports change date format frequently and the
    engine should accept any common shape without a config edit.

    Accepted automatically:
      - datetime / date cells (real Excel date types)
      - ISO: 2026-04-25, 2026/04/25, 20260425
      - US:  4/25/2026, 04/25/2026, 4/25/26
      - UK / IN: 25/4/2026, 25-04-2026
      - Month-name: 25 Apr 2026, 25-Apr-2026, Apr 25 2026, 25 April 2026
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date_type):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # Excel date serial (days since 1899-12-30), e.g. from a CSV.
        if 20000 < float(v) < 80000:
            return (datetime(1899, 12, 30) + timedelta(days=int(v))).date()
        raise TypeError(f"unsupported date value: {v!r}")
    if not isinstance(v, str):
        raise TypeError(f"unsupported date value: {v!r} ({type(v).__name__})")
    s = v.strip()
    if not s:
        return None
    # A date with a time part ('2026-09-26 00:00:00', '26/09/2026 05:05')
    # is still a date.
    if " " in s and ":" in s.split(" ", 1)[1]:
        s = s.split(" ", 1)[0]
    # Configured format first, then the fallback list.
    for candidate in (fmt, *_DATE_FORMAT_FALLBACKS):
        try:
            return datetime.strptime(s, candidate).date()
        except ValueError:
            continue
    raise ValueError(
        f"date {s!r} did not match {fmt!r} or any common fallback. "
        f"Add the format to _DATE_FORMAT_FALLBACKS in io/readers.py."
    )


_TIME_FORMATS: tuple[str, ...] = (
    "%H:%M", "%H:%M:%S", "%H.%M", "%H%M", "%I:%M %p", "%I:%M%p",
    "%I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M", "%d-%m-%Y %H:%M",
)


def _parse_time(v: Any) -> time | None:
    """A cell to a time of day. Besides real time cells and 'HH:MM',
    accepts '05:05:00', '05.05', '0505' / '505', '5:05 AM', a date+time
    string, and Excel's numeric time (a fraction of a day)."""
    if v is None:
        return None
    if isinstance(v, time):
        return v
    if isinstance(v, datetime):
        return v.time()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        frac = float(v) % 1
        if float(v) >= 1 and frac == 0 and float(v) < 2400:
            v = f"{int(v):04d}"           # 505 / 1325 read as a number
        else:
            minutes = round(frac * 24 * 60) % (24 * 60)
            return time(minutes // 60, minutes % 60)
    if isinstance(v, str):
        s = v.strip().upper().replace("HRS", "").replace("H", ":").strip()
        if s.isdigit() and len(s) in (3, 4):
            s = s.zfill(4)
        for fmt in _TIME_FORMATS:
            try:
                return datetime.strptime(s, fmt).time()
            except ValueError:
                continue
        raise ValueError(f"unparseable time: {v!r}")
    raise TypeError(f"unsupported time value: {v!r} ({type(v).__name__})")


def normalize_status(raw: Any, status_cfg: StatusConfig) -> tuple[CrewStatus, str]:
    """Normalize a roster cell value to (CrewStatus, original_literal).

    Per user direction 2026-05-10: accept ZC/M, M/ZC, zc/m, P2F/M,
    M/P2F, p2f/m — any order and any case. Implementation: try direct
    aliases first, then a canonical form (uppercased, slash-parts
    sorted alphabetically) so 'p2f/m' and 'M/P2F' both map to the
    canonical 'M/P2F' which is the enum value for CrewStatus.P2F_M.
    """
    if raw is None:
        return CrewStatus.BLANK, ""
    literal = str(raw).strip()
    if literal == "":
        return CrewStatus.BLANK, ""
    # 1) Direct enum / alias match (covers all literal CrewStatus values).
    aliased = status_cfg.aliases.get(literal, literal)
    try:
        return CrewStatus(aliased), literal
    except ValueError:
        pass
    # 2) Canonical form: uppercase, split on '/', sort alphabetically.
    #    'P2F/m' / 'p2f/M' / 'M/p2f' all become 'M/P2F'.
    if "/" in literal:
        parts = sorted(p.strip().upper() for p in literal.split("/") if p.strip())
        canonical = "/".join(parts)
        # Try alias first then enum.
        aliased2 = status_cfg.aliases.get(canonical, canonical)
        try:
            return CrewStatus(aliased2), literal
        except ValueError:
            pass
    # 3) Last resort: uppercase only.
    upper = literal.upper()
    aliased3 = status_cfg.aliases.get(upper, upper)
    try:
        return CrewStatus(aliased3), literal
    except ValueError:
        pass
    return CrewStatus.OTHER, literal


_SHIFT_CODES_TUPLE: tuple[str, ...] = ("M", "A", "N", "M1", "A1")


def _coerce_shift(v: Any) -> ShiftCode | None:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in _SHIFT_CODES_TUPLE:
        return s  # type: ignore[return-value]
    return None


# ---------- IN_SVportal ----------

# Built-in header aliases (2026-10-10). Matched after ``norm_header``, so
# case, dots, underscores, brackets and spacing do not matter. Names in
# config.yml ``io.sv_portal.columns`` always take precedence; these only
# catch the usual relabels of an export so a minor format change does not
# silently lose a column.
_SV_ALIASES: dict[str, tuple[str, ...]] = {
    "flight_id": (
        "flt", "flight", "flight no", "flight number", "flt no", "fltno",
        "flight num", "flight nbr",
    ),
    "departure": (
        "dep", "origin", "from", "departure", "dep stn", "dep station",
        "departure station", "board point", "sector from",
    ),
    "arrival": (
        "arr", "dest", "destination", "to", "arrival", "arr stn",
        "arr station", "arrival station", "off point", "sector to",
    ),
    "dep_time": (
        "std", "dep", "std lt", "std local", "dep time", "departure time",
        "sched dep", "scheduled departure", "std time",
    ),
    "arr_time": ("sta", "sta lt", "arr time", "arrival time", "scheduled arrival"),
    "date": ("date", "flight date", "flt date", "dep date", "departure date", "flt dt"),
    "booked_pax_raw": ("load", "booked pax", "pax load", "pax", "booked"),
    "aircraft_type": ("type", "flt type", "flight type"),
    "aircraft_subtype": ("a c type", "ac type", "aircraft", "ac", "equipment"),
    "owner": ("aircraft owner", "owner", "operator"),
}
# 'dep' is both the old DEP (station) header and the short form of
# "Dep." (time) in the new export; the config map settles which one a
# given file means, so it is left out of the dep_time aliases below.
_SV_ALIASES["dep_time"] = tuple(a for a in _SV_ALIASES["dep_time"] if a != "dep")

#: Columns the engine cannot work without.
SV_REQUIRED_FIELDS: tuple[str, ...] = (
    "flight_id", "departure", "arrival", "dep_time", "date",
)
_SV_FIELD_LABEL = {
    "flight_id": "flight number", "departure": "departure station",
    "arrival": "arrival station", "dep_time": "departure time (STD)",
    "date": "date",
}


def _sv_column_map(
    header: Sequence[Any], config_columns: Mapping[str, str],
) -> dict[str, int]:
    """field -> column index for one candidate header row.

    Each header is matched in tiers, strictest first: the exact config
    name, the config name ignoring case, the config name ignoring case
    and punctuation (only when that points at a single field — config
    has both 'DEP' = station and 'Dep.' = time), then the built-in
    aliases. A field keeps its strictest, then left-most, match."""
    exact = dict(config_columns)
    lower = {k.strip().lower(): v for k, v in config_columns.items()}
    by_norm: dict[str, set[str]] = {}
    for k, v in config_columns.items():
        by_norm.setdefault(norm_header(k), set()).add(v)
    alias_norm = {a: f for f, names in _SV_ALIASES.items() for a in names}
    best: dict[str, tuple[int, int]] = {}   # field -> (tier, column)
    for i, h in enumerate(header):
        if h is None:
            continue
        raw = str(h).strip()
        key = norm_header(raw)
        if not key:
            continue
        hit: tuple[int, str] | None = None
        if raw in exact:
            hit = (0, exact[raw])
        elif raw.lower() in lower:
            hit = (1, lower[raw.lower()])
        elif len(by_norm.get(key, ())) == 1:
            hit = (2, next(iter(by_norm[key])))
        elif key in alias_norm and key not in by_norm:
            hit = (3, alias_norm[key])
        if hit is None:
            continue
        tier, field_name = hit
        if field_name not in best or tier < best[field_name][0]:
            best[field_name] = (tier, i)
    return {f: col for f, (_tier, col) in best.items()}


def find_sv_header(
    rows: Sequence[Sequence[Any]], config_columns: Mapping[str, str],
) -> tuple[int, dict[str, int]]:
    """(header row index, field -> column) — the row within the first
    ``_HEADER_SCAN_ROWS`` that names the most known columns, so a title
    banner or blank lines above the header are skipped."""
    best = (0, _sv_column_map(rows[0], config_columns) if rows else {})
    for i, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        found = _sv_column_map(row, config_columns)
        if len(found) > len(best[1]):
            best = (i, found)
    return best


def read_sv_portal(wb: Workbook, config: Config) -> list[RawFlightRow]:
    """Read the flight schedule (SV portal export).

    Tolerant of minor format changes (2026-10-10): the header row is
    found within the first rows, header labels are matched ignoring case
    and punctuation, and common relabels are recognised (see
    ``_SV_ALIASES``); config ``io.sv_portal.columns`` still wins. A file
    missing a column the engine needs is refused with the list of what
    is missing instead of silently dropping every flight.

    Every cell is also kept in ``extra_columns`` under its literal header
    so the extraction filters can match on any column (REG, Crew, ...).
    """
    cfg = config.io.sv_portal
    ws = pick_sheet(wb, SHEET_IN_SVPORTAL)
    all_rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if not all_rows:
        raise ValueError("the flight schedule sheet is empty")
    header_idx, idx_by_field = find_sv_header(all_rows, cfg.columns)
    missing = [f for f in SV_REQUIRED_FIELDS if f not in idx_by_field]
    if missing:
        seen = [str(h).strip() for h in all_rows[header_idx] if h not in (None, "")]
        raise ValueError(
            "the flight schedule is missing "
            + ", ".join(_SV_FIELD_LABEL[f] for f in missing)
            + f" (headers found: {', '.join(seen[:25]) or 'none'}). "
            "Rename the column, or add its name under io.sv_portal.columns "
            "in configs/config.yml."
        )
    header_row = all_rows[header_idx]
    header_by_idx: dict[int, str] = {
        i: str(h).strip() for i, h in enumerate(header_row)
        if h is not None and str(h).strip()
    }

    out: list[RawFlightRow] = []
    for row in all_rows[header_idx + 1:]:
        if not any(c not in (None, "") for c in row):
            continue
        kwargs: dict[str, Any] = {}
        for field_name, idx in idx_by_field.items():
            value = row[idx] if idx < len(row) else None
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            try:
                if field_name == "date":
                    kwargs[field_name] = _parse_date(value, cfg.date_format)
                elif field_name in ("dep_time", "arr_time"):
                    kwargs[field_name] = _parse_time(value)
                elif isinstance(value, str):
                    kwargs[field_name] = value.strip()
                elif isinstance(value, float) and value.is_integer():
                    # Excel keeps numbers as floats: FLT 1 arrives as 1.0
                    # and used to surface as "1.0" everywhere downstream.
                    kwargs[field_name] = int(value)
                else:
                    kwargs[field_name] = value
            except (ValueError, TypeError):
                # One odd cell (a note, a "TBA" time) skips that value;
                # Step 1 drops the row if it was the date or STD.
                continue
        extra: dict[str, str] = {}
        for idx, header in header_by_idx.items():
            if idx < len(row) and row[idx] is not None:
                extra[header] = str(row[idx]).strip()
        kwargs["extra_columns"] = extra
        out.append(RawFlightRow.model_validate(kwargs))
    return out


# ---------- IN_Staff / IN_AM_Roster (wide format) ----------

class DuplicateEmployeeIdError(ValueError):
    """Raised when IN_Staff or IN_AM_Roster contains the same Employee_ID
    on more than one data row.

    Maps to ``W020 ERROR`` in the warnings system. Step orchestrators catch
    this, write a W020 row to ``OUT_Warnings``, and return without producing
    downstream output — silently merging two distinct staff under one ID
    would let the solver double-allocate flights and corrupt workload
    summaries, which is worse than failing loudly.

    Attributes:
        sheet_label: 'IN_Staff' or 'IN_AM_Roster'.
        duplicates: dict mapping each duplicated Employee_ID to the list of
            crew names that share it.
    """

    def __init__(self, sheet_label: str, duplicates: dict[str, list[str]]) -> None:
        self.sheet_label = sheet_label
        self.duplicates = duplicates
        details = "; ".join(
            f"{eid!r} → {len(names)} rows ({', '.join(names)})"
            for eid, names in sorted(duplicates.items())
        )
        super().__init__(
            f"W020: duplicate Employee_ID(s) in {sheet_label}: {details}. "
            "Edit the Employee_ID column so each row has a unique value."
        )


def _check_unique_employee_ids(
    rows_with_ids: list[tuple[str, str]], sheet_label: str,
) -> None:
    """Raise ``DuplicateEmployeeIdError`` if any employee_id in
    ``rows_with_ids`` (a list of ``(employee_id, name)`` tuples) appears
    on more than one row.
    """
    seen: dict[str, list[str]] = {}
    for eid, name in rows_with_ids:
        seen.setdefault(eid, []).append(name)
    duplicates = {eid: names for eid, names in seen.items() if len(names) > 1}
    if duplicates:
        raise DuplicateEmployeeIdError(sheet_label, duplicates)


class MissingEmployeeIdError(ValueError):
    """Raised when a crew-roster row has a blank Employee_ID cell.

    Per user direction 2026-05-21: every roster row MUST carry an
    explicit Employee_ID so duplicate-name staff are unambiguously
    distinguishable downstream (override resolution, allocation
    readback, audit trail). The previous auto-gen ``STAFF_NNN`` /
    ``AM_NNN`` fallback was removed — silent fallback let same-named
    staff get conflated by anyone editing the override sheet.
    """

    def __init__(self, sheet_label: str, blank_names: list[str]) -> None:
        self.sheet_label = sheet_label
        self.blank_names = blank_names
        preview = ", ".join(blank_names[:5])
        if len(blank_names) > 5:
            preview += f", … (+{len(blank_names) - 5} more)"
        super().__init__(
            f"W021: {len(blank_names)} row(s) in {sheet_label} have "
            f"a blank Employee_ID: {preview}. Every staff row MUST "
            "carry an Employee_ID — open the workbook, fill the column, "
            "save, and re-run."
        )

# ---- header vocabulary for the wide rosters -------------------------------
#
# The monthly staff roster the rostering team publishes looks like:
#
#     row 1   CLC STAFF ROSTER FROM 31 AUG TILL 27 SEP 2026   (banner)
#     row 2   S.no | Name | IGA | add info | Contact | Mon,31Aug | Tue,1Sep | ...
#     row 3+  1 | YOGESH BADHAN | 10001 | P2F/Corendon | 78389... | F | A | ...
#
# so the reader must (a) find the header row instead of assuming row 1,
# and (b) accept the names that file uses for each column. Every alias
# list is matched case-insensitively and the configured name always wins.

# Matched with ``norm_header`` — case, dots, underscores and spacing do
# not matter ('IGA No.' == 'iga_no' == 'IGA  NO').
_NAME_ALIASES: tuple[str, ...] = (
    "Name", "STAFF NAME", "Employee Name", "Crew Name", "Full Name",
    "Name of Staff", "Name of Employee", "Agent Name",
)
_ID_ALIASES: tuple[str, ...] = (
    "ID", "Employee_ID", "EmployeeID", "Employee ID", "IGA", "IGA No",
    "IGA ID", "IGA Number", "IGA Code", "Emp ID", "Emp No", "Emp Code",
    "Employee No", "Employee Number", "Employee Code", "Staff ID",
    "Staff No", "Staff Code",
)
_LICENSE_ALIASES: tuple[str, ...] = (
    "License", "Licence", "add info", "Additional Info", "Addl Info",
    "Licence Type", "License Type", "Licenses", "Licences",
)

# Rows above the header (a title banner, a blank spacer) are skipped;
# don't scan the whole sheet for one.
_HEADER_SCAN_ROWS = 15
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _normalize_license(value: Any) -> str | None:
    """'P2F/Corendon' -> 'P2F+Corendon'; 'P2F/' -> 'P2F'; '/' or blank -> None.

    The roster's "add info" column separates qualifications with '/', and
    uses a bare '/' for "none". Downstream code only asks whether the
    string contains 'P2F', so the joined form keeps that working while
    the placeholder slash stops being a fake licence.
    """
    if value is None:
        return None
    parts = [p.strip() for p in re.split(r"[/+,]", str(value)) if p.strip()]
    return "+".join(parts) if parts else None


def _find_header_row(
    rows: Sequence[Sequence[Any]], name_candidates: Sequence[str],
) -> int:
    """Index of the first row (within the scan window) that has a
    name-column label in it; 0 when none does, so the caller's existing
    'name column not in header' error still fires with the first row."""
    wanted = {norm_header(n) for n in name_candidates}
    for i, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        for cell in row:
            if isinstance(cell, str) and norm_header(cell) in wanted:
                return i
    return 0


_WEEKDAY_RE = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?$", re.I)


def _looks_like_subheader(
    row: Sequence[Any], name_idx: int, date_cols: Iterable[int],
) -> bool:
    """A weekday strip under the header ('Mon Tue Wed ...') or a row with
    no name in it — as opposed to the first real staff row."""
    name = row[name_idx] if name_idx < len(row) else None
    if not (isinstance(name, str) and name.strip()):
        return True
    cells = [row[c] for c in date_cols if c < len(row) and row[c] not in (None, "")]
    return bool(cells) and all(
        isinstance(c, str) and _WEEKDAY_RE.match(c.strip()) for c in cells
    )


def _banner_year(rows: Sequence[Sequence[Any]], header_idx: int) -> int | None:
    """A 4-digit year mentioned in the rows above the header, if any."""
    for row in rows[:header_idx]:
        for cell in row:
            if isinstance(cell, str):
                m = _YEAR_RE.search(cell)
                if m:
                    return int(m.group(1))
    return None


def _resolve_date_columns(
    header: Sequence[Any], fmt: str, ref_year: int,
    anchor: date_type | None = None,
) -> dict[int, date_type]:
    """Map column index -> date for every date-looking header cell.

    Headers like 'Mon,31Aug' carry no year, and a roster can run across
    New Year, so the year is *derived*, not configured:

      * with a weekday in the header ('Mon,31Aug') the year is the one,
        within a year of ``ref_year``, in which 31 Aug is a Monday;
      * otherwise the year starts at ``ref_year`` and rolls forward
        whenever the dates step backwards (Dec -> Jan).

    ``anchor`` (the workbook's last-modified date) settles the year for
    weekday-less headers like '26-May': the first date takes whichever
    year lands it nearest the anchor, so a roster keeps resolving
    correctly year after year without a config edit.

    Real Excel date cells are taken as they are.
    """
    has_weekday = "%a" in fmt or "%A" in fmt
    month_first = _numeric_headers_month_first(header, ref_year)
    out: dict[int, date_type] = {}
    prev: date_type | None = None
    for i, h in enumerate(header):
        d: date_type | None = None
        if isinstance(h, datetime):
            d = h.date()
        elif isinstance(h, date_type):
            d = h
        elif isinstance(h, str) and h.strip():
            s = h.strip()
            base = prev.year if prev else ref_year
            years = [base, base + 1, base - 1]
            if has_weekday:
                # The weekday name pins the year down; try nearby years.
                fallback: date_type | None = None
                for y in years:
                    try:
                        dt = datetime.strptime(f"{s} {y}", f"{fmt} %Y")
                    except ValueError:
                        continue
                    fallback = fallback or dt.date()
                    if dt.strftime("%a").lower() == s[:3].lower():
                        d = dt.date()
                        break
                if d is None:
                    d = fallback
            elif prev is None and anchor is not None:
                best: date_type | None = None
                for y in (anchor.year - 1, anchor.year, anchor.year + 1):
                    try:
                        cand = datetime.strptime(f"{s} {y}", f"{fmt} %Y").date()
                    except ValueError:
                        continue
                    if best is None or abs((cand - anchor).days) < abs((best - anchor).days):
                        best = cand
                d = best
            else:
                for y in years[:2]:
                    try:
                        dt = datetime.strptime(f"{s} {y}", f"{fmt} %Y")
                    except ValueError:
                        continue
                    d = dt.date()
                    if prev is not None and d < prev:
                        continue          # went backwards: try next year
                    break
            if d is None:
                # Not in the configured shape: try the loose reader so a
                # minor relabel ('Mon, 31 Aug', '31 Aug', '31/08',
                # '31-Aug-26') is still a date column.
                d = _loose_header_date(
                    s, prev.year if prev else ref_year, prev, anchor,
                    month_first=month_first,
                )
        if d is not None:
            out[i] = d
            prev = d
    return out


_MONTHS = {
    m: i for i, names in enumerate((
        (), ("jan", "january"), ("feb", "february"), ("mar", "march"),
        ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
        ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
        ("nov", "november"), ("dec", "december"),
    )) for m in names
}
_WEEKDAYS = {
    w: i for i, names in enumerate((
        ("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"),
        ("thu", "thur", "thurs", "thursday"), ("fri", "friday"),
        ("sat", "saturday"), ("sun", "sunday"),
    )) for w in names
}


def _numeric_headers_month_first(header: Sequence[Any], year: int) -> bool:
    """For all-number day headers ('09/01/2026'), whether they are
    month-first (US) rather than day-first. A sheet saved on a US-locale
    PC writes 08/31, 09/01, ...; an Indian one 31/08, 01/09, .... Read
    both ways and keep the one whose columns run as consecutive days."""
    pairs: list[tuple[int, int, int | None]] = []
    for h in header:
        if not isinstance(h, str):
            continue
        tokens = re.findall(r"\d+|[A-Za-z]+", h)
        if 2 <= len(tokens) <= 3 and all(t.isdigit() for t in tokens):
            y = int(tokens[2]) if len(tokens) == 3 else None
            pairs.append((int(tokens[0]), int(tokens[1]), y))
    if len(pairs) < 2:
        return False

    def run_score(first_is_day: bool) -> int:
        dates: list[date_type] = []
        for a, b, y in pairs:
            day, month = (a, b) if first_is_day else (b, a)
            yy = (y + 2000 if y is not None and y < 100 else y) or year
            try:
                dates.append(date_type(yy, month, day))
            except ValueError:
                dates.append(None)  # type: ignore[arg-type]
        return sum(
            1 for x, z in zip(dates, dates[1:])
            if x is not None and z is not None and (z - x).days == 1
        )

    return run_score(False) > run_score(True)


def _loose_header_date(
    s: str, base_year: int, prev: date_type | None, anchor: date_type | None,
    *, month_first: bool = False,
) -> date_type | None:
    """A roster column header in any common day/month shape, or None.

    Splits the label into words and numbers ('Mon, 31 Aug' -> mon 31
    aug; '31/08' -> 31 08; '31Aug26' -> 31 aug 26) and needs a valid day
    and month; a weekday word, when present, picks the year. Labels like
    'Name', 'S.no' or 'Contact' have no day+month and are ignored.
    """
    tokens = re.findall(r"\d+|[A-Za-z]+", s)
    if not tokens or len(tokens) > 4:
        return None
    weekday: int | None = None
    month: int | None = None
    nums: list[int] = []
    for t in tokens:
        tl = t.lower()
        if tl.isdigit():
            nums.append(int(tl))
        elif tl in _WEEKDAYS and weekday is None:
            weekday = _WEEKDAYS[tl]
        elif tl in _MONTHS and month is None:
            month = _MONTHS[tl]
        else:
            return None                      # any other word: not a date
    year: int | None = None
    if month is None:
        if len(nums) < 2:
            return None
        day, month = nums[0], nums[1]        # day-first (31/08)
        if month_first or (month > 12 and day <= 12):
            day, month = month, day          # 08/31
        rest = nums[2:]
    else:
        if not nums:
            return None
        day, rest = nums[0], nums[1:]
    if rest:
        y = rest[0]
        year = y + 2000 if y < 100 else y
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None

    def make(y: int) -> date_type | None:
        try:
            return date_type(y, month, day)
        except ValueError:
            return None

    if year is not None:
        return make(year)
    candidates = [c for c in (make(y) for y in (base_year, base_year + 1, base_year - 1)) if c]
    if not candidates:
        return None
    if weekday is not None:
        for c in candidates:
            # A weekday typo must not throw the column a year backwards.
            if c.weekday() == weekday and (prev is None or c >= prev):
                return c
    if prev is None and anchor is not None:
        return min(candidates, key=lambda c: abs((c - anchor).days))
    first = candidates[0]
    if prev is not None and first < prev and len(candidates) > 1:
        return candidates[1]                 # Dec -> Jan rolls forward
    return first


def _find_header_idx(header: list[Any], *names: str) -> int | None:
    """Return the column index of the first header in ``names`` that matches
    case-insensitively, else None. Used so the reader can pick up
    Employee_ID / License columns whether the assigner labelled them
    'Employee_ID', 'employee_id', 'EmployeeID', etc."""
    norm_names = {norm_header(n) for n in names}
    for i, h in enumerate(header):
        if isinstance(h, str) and norm_header(h) in norm_names:
            return i
    return None


def _read_wide_roster_sheet(
    ws: Worksheet,
    *,
    name_column: str,
    date_header_format: str,
    cycle_year: int | None,
    skip_subheader_rows: int,
    status_cfg: StatusConfig,
    id_prefix: str,
) -> list[tuple[str, str | None, str, dict[date_type, CrewStatus], dict[date_type, str]]]:
    """Yield (employee_id, license, name, statuses, raw_statuses) per data row.

    If the sheet has an ``Employee_ID`` header column, its value is used. If
    blank or missing, the reader auto-generates ``{id_prefix}_NNN`` based on
    the sheet row index (1-based, header row counted). This keeps IDs stable
    across runs as long as the assigner doesn't reorder rows.

    Same pattern for ``License``: blank → None.
    """
    all_rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if not all_rows:
        raise ValueError("uploaded roster sheet is empty")
    name_candidates = [name_column, *_NAME_ALIASES]
    header_idx = _find_header_row(all_rows, name_candidates)
    header = all_rows[header_idx]
    body = all_rows[header_idx + 1:]
    # Case-insensitive name-column match; the configured label first,
    # then the aliases the real files use.
    name_idx: int | None = None
    for cand in name_candidates:
        target = norm_header(cand)
        for i, h in enumerate(header):
            if isinstance(h, str) and norm_header(h) == target:
                name_idx = i
                break
        if name_idx is not None:
            break
    if name_idx is None:
        raise ValueError(
            f"name column {name_column!r} (case-insensitive) "
            f"not in header {header}"
        )
    # Canonical ID header is "ID"; "IGA" is what the monthly roster uses.
    id_idx = _find_header_idx(header, *_ID_ALIASES)
    lic_idx = _find_header_idx(header, *_LICENSE_ALIASES)
    # The year for headers without one: a year written above the header
    # wins, then a configured override, then the file's own date, then
    # today. Weekdays in the headers ('Mon,30Mar') then pin the exact
    # year within one either side, so nothing needs editing in January.
    banner = _banner_year(all_rows, header_idx)
    anchor: date_type | None = None
    if banner is None:
        props = getattr(getattr(ws, "parent", None), "properties", None)
        stamp = getattr(props, "modified", None) or getattr(props, "created", None)
        anchor = stamp.date() if isinstance(stamp, datetime) else date_type.today()
    ref_year = banner or cycle_year or (anchor.year if anchor else date_type.today().year)
    date_by_idx = _resolve_date_columns(
        header, date_header_format, ref_year, anchor,
    )
    # The AM roster may or may not carry a weekday strip under the
    # header, so the configured skip count only applies to rows that
    # actually look like one — never to the first staff row.
    for _ in range(skip_subheader_rows):
        if body and _looks_like_subheader(body[0], name_idx, date_by_idx):
            body = body[1:]
        else:
            break
    rows_iter = iter(body)
    # Per user direction 2026-05-21: Employee_ID is now mandatory.
    # Header must exist AND every row must have a non-blank value.
    # The previous auto-gen fallback (``STAFF_NNN`` / ``AM_NNN``) was
    # removed so two same-named staff can never silently share a
    # solver identity — the roster team has to assign distinct IDs.
    sheet_label = ws.title if hasattr(ws, "title") else "<sheet>"
    if id_idx is None:
        raise MissingEmployeeIdError(sheet_label, ["<ID / IGA column missing>"])
    out: list[tuple[str, str | None, str, dict[date_type, CrewStatus], dict[date_type, str]]] = []
    blank_id_names: list[str] = []
    for row in rows_iter:
        if name_idx >= len(row):
            continue
        name_val = row[name_idx]
        if not isinstance(name_val, str) or not name_val.strip():
            continue
        emp_id = ""
        if id_idx < len(row):
            v = row[id_idx]
            if isinstance(v, str) and v.strip():
                emp_id = v.strip()
            elif isinstance(v, float) and v.is_integer():
                emp_id = str(int(v))          # 10001.0 -> "10001"
            elif v is not None and not isinstance(v, str):
                emp_id = str(v).strip()
        if not emp_id:
            # A footer / note line (text in the name column, nothing in
            # any date column) is not a staff row; a real staff row
            # without an ID still fails loudly below.
            has_status = any(
                c < len(row) and row[c] not in (None, "")
                for c in date_by_idx
            )
            if has_status:
                blank_id_names.append(name_val.strip())
            continue
        # License: from cell if present and non-blank, else None.
        license_val: str | None = None
        if lic_idx is not None and lic_idx < len(row):
            license_val = _normalize_license(row[lic_idx])
        statuses: dict[date_type, CrewStatus] = {}
        raw_statuses: dict[date_type, str] = {}
        for col_idx, d in date_by_idx.items():
            if col_idx >= len(row):
                continue
            normalized, literal = normalize_status(row[col_idx], status_cfg)
            statuses[d] = normalized
            raw_statuses[d] = literal
        out.append((emp_id, license_val, name_val.strip(), statuses, raw_statuses))
    if blank_id_names:
        raise MissingEmployeeIdError(sheet_label, blank_id_names)
    return out


def read_staff_roster(wb: Workbook, config: Config) -> list[CrewRosterRow]:
    cfg = config.io.staff_roster
    rows = _read_wide_roster_sheet(
        pick_sheet(wb, SHEET_IN_STAFF),
        name_column=cfg.name_column,
        date_header_format=cfg.date_header_format,
        cycle_year=config.allocation.cycle_year,
        skip_subheader_rows=0,
        status_cfg=config.status,
        id_prefix="STAFF",
    )
    out = [
        CrewRosterRow(
            employee_id=eid, name=n, role=Role.STAFF, license=lic,
            status_by_date=s, raw_status_by_date=r,
        )
        for eid, lic, n, s, r in rows
    ]
    _check_unique_employee_ids(
        [(r.employee_id, r.name) for r in out], "Staff roster",
    )
    return out


def _infer_am_role(
    raw_status_by_date: dict[date_type, str], status_cfg: StatusConfig,
) -> Role:
    am_set = set(status_cfg.am_shifts)
    zc_set = set(status_cfg.zc_shifts)
    has_am = any(lit in am_set for lit in raw_status_by_date.values())
    has_zc = any(lit in zc_set for lit in raw_status_by_date.values())
    if has_am:
        return Role.AM
    if has_zc:
        return Role.ZC
    # Defensive fallback per plan §1.5.1: anyone in the AM roster sheet is AM/ZC
    # by definition even if their visible window contains no /IGT or /ZC entries.
    return Role.AM


def read_am_roster(wb: Workbook, config: Config) -> list[AMRosterRow]:
    cfg = config.io.am_roster
    rows = _read_wide_roster_sheet(
        pick_sheet(wb, SHEET_IN_AM_ROSTER),
        name_column=cfg.name_column,
        date_header_format=cfg.date_header_format,
        cycle_year=config.allocation.cycle_year,
        skip_subheader_rows=cfg.subheader_rows_to_skip,
        status_cfg=config.status,
        id_prefix="AM",
    )
    out: list[AMRosterRow] = []
    for eid, lic, n, s, r in rows:
        role = _infer_am_role(r, config.status)
        out.append(AMRosterRow(
            employee_id=eid, name=n, role=role, license=lic,
            status_by_date=s, raw_status_by_date=r,
        ))
    _check_unique_employee_ids(
        [(r.employee_id, r.name) for r in out], "AM roster",
    )
    return out


# IN_Required reader removed 2026-05-21 alongside the IN_Required
# sheet itself — no callers left, and the schema RequiredStaffingRow
# is no longer used. The dashboard "Required / Gap" columns were
# dropped in the same change.


# ---------- overrides (in-memory) ----------
#
# Override rows used to live on an IN_Override worksheet. They now live
# in ``state.STATE.overrides`` as a list of dicts keyed by the column
# names in ``state.OVERRIDE_HEADERS``. Each reader below filters that
# list by the row's ``type`` and returns its own typed list, so the
# orchestrators never have to discriminate.
#
#   type=p2f                → P2FNomination     (date, shift, employee_id)
#   type=max_flights        → PerStaffOverride  (max_flights)
#   type=cutoff_time        → PerStaffOverride  (std_start / std_cutoff)
#   type=sick               → employee_id list
#   type=change_role        → {employee_id: new_role}
#   type=waive_h10_pair     → WaiveH10Pair
#   type=raise_cap          → RaiseCapForFlight
#   type=skip_p2f_buffer    → SkipP2FBuffer
#   type=skip_intl_removal  → SkipINTLRemoval

OverrideRows = Sequence[Mapping[str, Any]]


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


def _row_type(row: Mapping[str, Any]) -> str:
    """Lowercased ``type`` value for a row, or '' when blank."""
    return _cell(row, "type").lower()


def _row_employee_name(row: Mapping[str, Any]) -> str | None:
    """Staff name off the row. None when blank."""
    return _cell(row, "employee") or None


def _rows_of_type(overrides: OverrideRows, wanted: str) -> list[Mapping[str, Any]]:
    return [r for r in overrides if _row_type(r) == wanted]


def _resolve_name_to_id(
    name: str,
    name_to_id: dict[str, str] | None,
) -> str:
    """Resolve a staff name to an employee_id using the provided map.
    If no map is given, or the name isn't found, return the name as-is —
    Step 4 will surface a W212 if it can't find that staff in the
    roster anyway."""
    if not name_to_id:
        return name
    return name_to_id.get(name.strip().upper()) or name


def read_p2f_nominations(
    overrides: OverrideRows, d_day: date_type,
    name_to_id: dict[str, str] | None = None,
) -> list[P2FNomination]:
    """Read all rows where ``type=p2f``. Returns one P2FNomination per
    (date, shift). Date is stamped from ``d_day``; the employee column
    is a name, resolved via ``name_to_id`` when provided."""
    from ..schemas import OverrideType
    out: list[P2FNomination] = []
    for row in _rows_of_type(overrides, OverrideType.P2F.value):
        s = _coerce_shift(_cell(row, "shift"))
        name = _row_employee_name(row)
        if s is None or not name:
            continue
        out.append(P2FNomination(
            date=d_day, shift=s,
            employee_id=_resolve_name_to_id(name, name_to_id),
        ))
    return out


def read_p2f_exclusions(
    overrides: OverrideRows,
    name_to_id: dict[str, str] | None = None,
) -> set[str]:
    """Read ``type=p2f_exclude`` rows: employee_ids (or names, when no
    map is given) who must NOT act as a P2F handler today."""
    from ..schemas import OverrideType
    out: set[str] = set()
    for row in _rows_of_type(overrides, OverrideType.P2F_EXCLUDE.value):
        name = _row_employee_name(row)
        if name:
            out.add(_resolve_name_to_id(name, name_to_id))
    return out


def read_per_staff_overrides(
    overrides: OverrideRows, d_day: date_type,
    name_to_id: dict[str, str] | None = None,
) -> list[PerStaffOverride]:
    """Read max_flights / cutoff_time rows. Each row produces one
    PerStaffOverride; the field populated depends on the row's type.

    cutoff_time rows may carry a ``from_time`` (start of the custom
    window) alongside ``to_time`` / ``limit`` (the upper bound). Either
    may be blank; a row with both blank is skipped."""
    import contextlib
    from ..schemas import OverrideType
    out: list[PerStaffOverride] = []
    _PER_STAFF_TYPES = (
        OverrideType.MAX_FLIGHTS.value,
        OverrideType.CUTOFF_TIME.value,
    )
    for row in overrides:
        rt = _row_type(row)
        if rt not in _PER_STAFF_TYPES:
            continue
        name = _row_employee_name(row)
        if not name:
            continue
        eid = _resolve_name_to_id(name, name_to_id)
        max_flights: int | None = None
        std_cutoff: time | None = None
        std_start: time | None = None
        if rt == OverrideType.MAX_FLIGHTS.value:
            v = _cell(row, "limit")
            if not v:
                continue
            with contextlib.suppress(TypeError, ValueError):
                max_flights = int(float(v))
            if max_flights is None:
                continue
        elif rt == OverrideType.CUTOFF_TIME.value:
            v_to = _cell(row, "to_time", "std_cutoff", "limit")
            if v_to:
                with contextlib.suppress(TypeError, ValueError):
                    std_cutoff = _parse_time(v_to)
            v_from = _cell(row, "from_time", "std_start")
            if v_from:
                with contextlib.suppress(TypeError, ValueError):
                    std_start = _parse_time(v_from)
            # Nothing to enforce if both bounds are blank.
            if std_cutoff is None and std_start is None:
                continue
        try:
            out.append(PerStaffOverride(
                employee_id=eid, date=d_day,
                max_flights=max_flights, std_cutoff=std_cutoff,
                std_start=std_start, is_newbie=False,
            ))
        except ValueError:
            continue
    return out


def read_role_change_overrides(
    overrides: OverrideRows, d_day: date_type,
    name_to_id: dict[str, str] | None = None,
) -> dict[str, str]:
    """Read ``type=change_role`` rows. Returns ``{employee_id: new_role}``.

    The ``shift`` column carries the new role label (STAFF / ZC / AM) —
    it is otherwise unused for this row type.

    Engine semantics (step3):
      - STAFF -> ZC: bucket switches to the ZC band (target 14-15 for
        day shifts, 11 for N). Extra flights above that target get
        redistributed via the pin re-solve.
      - STAFF/ZC -> AM: shift_today set to None so the staff becomes
        ineligible for any flight; everything they were carrying
        redistributes.
      - AM -> STAFF/ZC: AM staff who weren't in the assignable pool now
        ARE; engine treats them as a fresh staff member on the resolved
        shift.

    Invalid new_role values are silently dropped — same as every other
    override reader.
    """
    from ..schemas import OverrideType
    out: dict[str, str] = {}
    for row in _rows_of_type(overrides, OverrideType.CHANGE_ROLE.value):
        name = _row_employee_name(row)
        new_role = _cell(row, "shift", "role").upper()
        if not name or new_role not in ("STAFF", "ZC", "AM"):
            continue
        out[_resolve_name_to_id(name, name_to_id)] = new_role
    return out


def read_sick_overrides(
    overrides: OverrideRows, d_day: date_type,
    name_to_id: dict[str, str] | None = None,
) -> list[str]:
    """Read ``type=sick`` rows. Returns employee_ids (or names, when no
    name_to_id map is given) flagged as off-duty for today.

    Step 3 applies these by zeroing the staff's shift_today before
    eligibility / pair generation, so their flights redistribute to the
    remaining staff on the same shift.
    """
    from ..schemas import OverrideType
    out: list[str] = []
    for row in _rows_of_type(overrides, OverrideType.SICK.value):
        name = _row_employee_name(row)
        if not name:
            continue
        out.append(_resolve_name_to_id(name, name_to_id))
    return out


# ---------- Phase R: per-flight relaxation override readers ----------
#
# All four discriminate on the ``type`` key. Common columns: ``flight``
# (FLT number) and ``std`` (STD of the flight being relaxed).
# Type-specific: waive_h10_pair → employee + other_std; raise_cap and
# skip_p2f_buffer → employee; skip_intl_removal → nothing more.


def _row_flight_std(
    row: Mapping[str, Any],
) -> tuple[str | None, time | None]:
    """Pull (flight, std) off an override row. (None, None) when either
    is missing or unparseable."""
    flight = _cell(row, "flight", "flt") or None
    std: time | None = None
    raw_std = _cell(row, "std")
    if raw_std:
        try:
            std = _parse_time(raw_std)
        except (TypeError, ValueError):
            std = None
    return flight, std


def read_waive_h10_pairs(
    overrides: OverrideRows, d_day: date_type,
    name_to_id: dict[str, str] | None = None,
) -> list[WaiveH10Pair]:
    """Read all ``type=waive_h10_pair`` rows."""
    from ..schemas import OverrideType
    out: list[WaiveH10Pair] = []
    for row in _rows_of_type(overrides, OverrideType.WAIVE_H10_PAIR.value):
        flight, std = _row_flight_std(row)
        name = _row_employee_name(row)
        if flight is None or std is None or not name:
            continue
        other_std: time | None = None
        raw_other = _cell(row, "other_std")
        if raw_other:
            try:
                other_std = _parse_time(raw_other)
            except (TypeError, ValueError):
                other_std = None
        if other_std is None:
            continue
        try:
            out.append(WaiveH10Pair(
                date=d_day, flight=flight, std=std,
                employee_id=_resolve_name_to_id(name, name_to_id),
                other_std=other_std,
            ))
        except ValueError:
            continue
    return out


def read_raise_cap_overrides(
    overrides: OverrideRows, d_day: date_type,
    name_to_id: dict[str, str] | None = None,
) -> list[RaiseCapForFlight]:
    """Read all ``type=raise_cap`` rows."""
    from ..schemas import OverrideType
    out: list[RaiseCapForFlight] = []
    for row in _rows_of_type(overrides, OverrideType.RAISE_CAP.value):
        flight, std = _row_flight_std(row)
        name = _row_employee_name(row)
        if flight is None or std is None or not name:
            continue
        try:
            out.append(RaiseCapForFlight(
                date=d_day, flight=flight, std=std,
                employee_id=_resolve_name_to_id(name, name_to_id),
            ))
        except ValueError:
            continue
    return out


def read_skip_p2f_buffer_overrides(
    overrides: OverrideRows, d_day: date_type,
    name_to_id: dict[str, str] | None = None,
) -> list[SkipP2FBuffer]:
    """Read all ``type=skip_p2f_buffer`` rows."""
    from ..schemas import OverrideType
    out: list[SkipP2FBuffer] = []
    for row in _rows_of_type(overrides, OverrideType.SKIP_P2F_BUFFER.value):
        flight, std = _row_flight_std(row)
        name = _row_employee_name(row)
        if flight is None or std is None or not name:
            continue
        try:
            out.append(SkipP2FBuffer(
                date=d_day, flight=flight, std=std,
                employee_id=_resolve_name_to_id(name, name_to_id),
            ))
        except ValueError:
            continue
    return out


def read_skip_intl_removal_overrides(
    overrides: OverrideRows, d_day: date_type,
) -> list[SkipINTLRemoval]:
    """Read all ``type=skip_intl_removal`` rows. Per-flight only — the
    override applies regardless of which handler took the INTL flight."""
    from ..schemas import OverrideType
    out: list[SkipINTLRemoval] = []
    for row in _rows_of_type(overrides, OverrideType.SKIP_INTL_REMOVAL.value):
        flight, std = _row_flight_std(row)
        if flight is None or std is None:
            continue
        try:
            out.append(SkipINTLRemoval(date=d_day, flight=flight, std=std))
        except ValueError:
            continue
    return out
