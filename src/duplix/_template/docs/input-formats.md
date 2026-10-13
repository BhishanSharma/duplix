# Input files

Three files go in: the day's **flight schedule** (SV portal export, on the
dashboard) and two period **rosters** (Staff, AM + ZC; in Setup).

## Accepted file types

All three uploads accept:

| File | Notes |
|---|---|
| `.xlsx`, `.xlsm` | as is |
| `.xls` (Excel 97–2003) | needs the `xlrd` package (in `requirements.txt`) |
| `.csv`, `.tsv`, `.txt` | comma, tab, semicolon or `|` separated; detected automatically |
| "Excel export" that is really text or an HTML table | common from web portals; detected by content, not by name |

The type is recognised from the file's content and the upload is stored
as a normal `.xlsx` (`src/io/upload_formats.py`). Any sheet name works:
each reader takes its expected sheet (`IN_SVportal`, `IN_Staff`,
`IN_AM_Roster`) if present, otherwise the first sheet.

## Flight schedule

**Required columns** — flight number, departure station, arrival station,
departure time (STD) and date. A file missing any of them is refused at
upload, naming what is missing. Other columns are kept and can be used by
extraction filters.

**Tolerated changes**

* The header row may sit below a title or blank rows (the first 15 rows
  are searched).
* Header names are matched ignoring case, dots, underscores, brackets and
  spacing: `Flight No.` = `FLIGHT_NO` = `flight no`.
* Common relabels are recognised:

  | Column | Also recognised as |
  |---|---|
  | flight number | `FLT`, `Flight`, `Flight No`, `Flight Number`, `Flt No` |
  | departure | `DEP`, `Origin`, `From`, `Departure`, `Dep Stn`, `Board Point` |
  | arrival | `ARR`, `Dest`, `Destination`, `To`, `Arrival`, `Arr Stn`, `Off Point` |
  | STD | `STD`, `Dep.`, `STD (LT)`, `Dep Time`, `Departure Time` |
  | date | `DATE`, `Flight Date`, `Flt Date`, `Dep Date` |
  | pax | `LOAD`, `Booked Pax`, `Pax Load`, `Pax` |
  | type / aircraft / owner | `TYPE`, `A/C Type`, `AC`, `Aircraft Owner`, `Owner` |

  Names listed under `io.sv_portal.columns` in `configs/config.yml` always
  win; add a new export's header there if it isn't recognised.
* **Times**: `05:30`, `5:30`, `05:30:00`, `05.30`, `0530`, `530`,
  `5:30 AM`, a date-and-time, or an Excel time cell.
* **Dates**: Excel date cells, `09/26/2026` (the configured US order is
  tried first), `26/09/2026`, `2026-09-26`, `26-Sep-2026`, `26 Sep 2026`,
  a date-and-time, or an Excel date number.
* Station codes may carry a terminal (`BOM-2`); whole numbers lose the
  Excel `.0` (`FLT 1`, not `1.0`).

A row whose date or STD cannot be read is dropped at Plan; one odd cell in
another column (a note, `TBA`) is ignored.

## Rosters

* **Name column** — `Name`, `Staff Name`, `Employee Name`, `Crew Name`, …
* **ID column** (required on every staff row) — `IGA`, `ID`,
  `Employee ID`, `Emp No`, `Employee Code`, `Staff ID`, …
* **Licence column** (optional) — `add info`, `License`, `Licence`, …
  (`P2F/Corendon` → P2F + Corendon; `/` = none).

All matched ignoring case and punctuation. The header row may sit below a
banner.

**Day columns** may be written as `Mon,31Aug`, `Mon, 31 Aug`, `Monday 31
Aug`, `31-Aug`, `31 Aug`, `31Aug`, `31/08`, `31-Aug-26`, `31/08/2026` or a
real date cell. For all-number headers both day-first and month-first are
tried, and the one that makes the columns run day after day is used. The
year comes from a date cell, the weekday, a year in the banner, or the
file's date, so a roster crossing New Year works.

**Status cells** are matched in any order and case (`M/ZC` = `zc/m`,
`P2F/M` = `m/p2f`); aliases such as `OFF` → `F` are in `status.aliases` in
`config.yml`. Unknown statuses mean "not available".

A roster is refused at upload if it has no recognisable day columns, no ID
column, a staff row without an ID, or the same ID twice.
