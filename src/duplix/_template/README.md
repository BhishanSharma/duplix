# Flight Allocation

Engine + web console for IndiGo CLC flight allocation: it takes the day's
flight schedule and the staff rosters, and assigns every flight to a
handler so that work is shared evenly within each shift.

Python 3.11–3.14. No database, no Docker, no Node — the UI is a stdlib
`http.server` app on `127.0.0.1:8765` with plain HTML/CSS/JS.

**Everything goes in and comes out through the browser.** Files are
uploaded on the page; results are shown on the page and can be downloaded
as an `.xlsx`.

## Quick start (Windows)

1. Double-click `start.bat`.
2. The first run takes about a minute (it creates `.venv` and installs
   `requirements.txt`). After that it opens http://127.0.0.1:8765/ straight
   away.

Mac / Linux, or to run it by hand:

```
python3 -m venv .venv            # Python 3.11–3.14
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m src.web                # --port 9000 --no-browser also work
```

## Daily flow

0. **Once per period** — open **Setup** (top right) and upload the two
   roster files: the **Staff roster** and the **AM + ZC roster**. They are
   saved to `data/rosters/` and reloaded on every start.
1. **Upload** today's flight schedule (the SV portal export) on the
   dashboard. `.xlsx`, `.xls`, CSV and text exports all work, and minor
   layout changes are tolerated — see [docs/input-formats.md](docs/input-formats.md).
2. **Check the date** (top right). It comes from the server's clock;
   change it to plan another day.
3. **Plan** (~3 s) — cleans the flights and reads the rosters.
4. **Review** the dashboard: flights and staff per shift, charts, the
   staffing recommendation, and the P2F handler cards. Allocate stays
   disabled until every shift with P2F flights has a valid handler — see
   [docs/p2f-handler-gate.md](docs/p2f-handler-gate.md).
5. **Allocate** (~20 s on a full day).
6. **Adjust** in the **Override** drawer if needed — `p2f` handler,
   `sick`, `change_role`, `max_flights`, `cutoff_time`, add / remove a
   flight or a staff member — then **Allocate** again:
   * **Allocate** keeps everyone else's flights where they are and only
     re-places what changed (a few seconds). Changed rows are flagged on
     the Allocations tab.
   * **Reset**, upload the schedule again, then **Plan** + **Allocate**
     builds a fresh plan instead —
     slightly fewer spacing compromises, but most flights change hands.
7. **Download .xlsx** when you need a file to send or print.

Nothing is ever left unallocated: a flight that cannot be placed within
the rules goes to whoever breaks the fewest, and is listed on the
Warnings tab (W230) with what was relaxed.

## Rosters

* **Staff roster** — the monthly sheet with a banner row, then
  `S.no | Name | IGA | add info | Contact | Mon,31Aug | Tue,1Sep | ...`.
  `IGA` is the employee id; `add info` (`P2F/Corendon`, `P2F/`, `/`) is
  the licence.
* **AM + ZC roster** — `STAFF NAME | 26-May | 27-May | ... | ID`, statuses
  like `OFF`, `M/ZC`, `A/IGT`.

The header row is found automatically, and the year of each day column is
worked out from the weekday, a year in the banner, or the file date — so a
roster crossing New Year just works.

Several rosters can be stored at once. For allocation date **D**, the app
merges every stored roster that has a column for D or D+1 (a later upload
wins where two overlap). Uploading a new period adds it beside the old one;
each can be removed in Setup. If D or D+1 is not covered, Setup shows a
notice and Plan raises W010 / W011.

## Zone Controllers

The rosters don't say who is ZC. Pick them on the dashboard's **Zone
Controllers** panel (search, click to add, × to remove).

* The list is **saved per date** (`data/zc_lists.json`). A date without its
  own list **carries over** the most recent earlier one.
* Removing a ZC who is marked `/ZC` in a roster applies to that date only;
  they go back to what the roster says (STAFF or AM, keeping any P2F duty).
* Someone on the list who is off that day is skipped and shown greyed out.
* Edits apply immediately; re-run **Allocate** to use them.

## What is kept between restarts

| Kept on disk (`data/`, git-ignored) | Lost on restart |
|---|---|
| rosters, ZC lists, early-release history | today's flight schedule, override rows, results |

**Reset** clears the results and today's flight schedule; the saved
rosters and override rows stay. Set
`FLIGHT_ALLOC_DATA_DIR` to keep `data/` somewhere else.

## Configuration

* `configs/config.yml` — column names of the SV export, status aliases,
  aircraft-type routing, solver time budget, break / relief / ZC cut-off
  minutes, international airport codes.
* `configs/shift_limits.json` — per shift and role: preferred / acceptable /
  hard cap flights (also editable in Setup).
* `configs/shift_times.json` — written by Setup when shift hours are
  changed; absent means the hours built into `src/allocator/windows.py`.
* `configs/zc_shift_times.json` — Zone Controllers' own hours, written by
  Setup; absent means ZCs follow the staff hours.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

## Documentation

| Doc | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | how the code is laid out, the pipeline, the HTTP API |
| [docs/allocation.md](docs/allocation.md) | the allocation rules, objective weights and post-passes |
| [docs/input-formats.md](docs/input-formats.md) | accepted files and tolerated format changes |
| [docs/p2f-handler-gate.md](docs/p2f-handler-gate.md) | P2F handler checks before Allocate |
| [docs/history.md](docs/history.md) | notable changes and the reasons behind them |
| [docs/handover.md](docs/handover.md) | state at handover, what needs attention and when, known limits |
