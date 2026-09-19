# Flight Allocation

Engine + web console for IndiGo CLC flight allocation. Python 3.11–3.14,
no database, no Docker, no Node — the UI is a zero-dependency stdlib
`http.server` app on `127.0.0.1:8765` with plain HTML/CSS/JS under
`src/web/static/`.

**Everything goes in and comes out through the browser.** The three
input files are uploaded in the browser and held in the server's memory
for the session; results are read back as JSON and, if you need a file
to send on, downloaded as a one-off `.xlsx`. There is no console
workbook, no `excel/` folder to manage, and no per-date file tree.

The files are split by how often they change. The flight schedule is a
fresh export every morning, so it sits on the dashboard. The two rosters
cover a whole period and are set once, in the **Setup** sidebar.

## Quick start (Windows)

1. Double-click `start.bat`.
2. First run takes ~1 minute: it creates a `.venv` and installs
   `requirements.txt`. Every run after that opens the browser straight
   to http://127.0.0.1:8765/.

## Manual setup (Mac/Linux, or to run it yourself)

```
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m src.cli                # --port 9000 --no-browser also work
```

## Daily flow

0. **Once per period** — open **Setup** (top right) and upload the two
   roster files. They stay loaded for the session, so this is not part
   of the daily flow:

   | Slot | What it is |
   |---|---|
   | Staff roster | regular crew roster |
   | AM + ZC roster | AM and ZC roster |

1. **Upload** today's flight schedule (the SV portal export) on the
   dashboard's *Today's flight schedule* card.

   Any sheet name works — each reader takes its canonical sheet if
   present and otherwise falls back to the first sheet in the file.

2. Check the allocation date (top right). It is prefilled from the
   **server's** date, not the browser's, so a machine on a stale clock
   can't plan the wrong day. Change it to back-fill another date.
3. Click **Plan** — cleans the flights and extracts the roster, ~3s.
4. Review the dashboard: per-shift stats, charts, roster by shift,
   staffing recommendation.
5. Click **Allocate** — CP-SAT solves, typically 30–90s on a first run.
6. Adjust anything that needs it from the **Override** drawer:
   - `p2f` / `norse` — handler nominations
   - `sick` — pull someone out of the day
   - `change_role` — promote STAFF→ZC, or push someone to AM
   - `max_flights` / `cutoff_time` — per-staff caps
   - add / remove a flight or a staff member
7. Click **Allocate** again. Prior assignments are pinned and used as a
   CP-SAT warm start, so only the affected staff's flights move and the
   re-solve takes seconds rather than a minute. Rows whose staff changed
   are flagged in the Allocations tab.
8. **Download .xlsx** when you want a file to email or print.

## Session lifetime

State lives in the server process. Restarting it clears the uploads,
the overrides and the results — start again by uploading the three
files. **Reset** is narrower: it clears the results but keeps your
uploads and override rows, so you can go straight back to Plan.

## Layout

```
src/
  state.py                  the in-memory store everything reads and writes
  plan.py                   Plan stage: step 1 + step 2 + the summary
  step1_clean_flights.py    clean the schedule into ops classes
  step2_extract_roster.py   rosters -> availability
  step3_allocate_flights.py solve, post-passes, outputs
  staged_overrides.py       apply drawer mutations to the working data
  recommender_staffing.py   per-shift required headcount
  schemas.py                the typed row models the whole app shares
  allocator/                eligibility, caps, pairings, post-passes
  solver/                   CP-SAT model
  io/
    readers.py              parse the uploaded workbooks + override rows
    export.py               render the state as a downloadable .xlsx
  web/
    server.py               the request loop + launcher (no endpoint logic)
    core/                   Response, the Router, static file serving
    api/                    one module per concern; each registers routes
    readback/               state -> the JSON the UI renders
    overrides/              override + config.yml mutations
    runner.py               runs a stage on a background thread
    static/
      index.html            a shell; views bring their own markup
      js/core/              dom, api, store, shared constants
      js/ui/                tab registry, layout mounting, modals
      js/views/             one module per tab (views/index.js registers)
      js/charts/            one module per dashboard chart
      js/panels/            dashboard blocks with their own load cycle
      js/drawer/            the Override drawer and its sections
      js/sidebar/           the Setup sidebar (the set-once roster files)
      css/                  partials, listed in styles.css
configs/
  config.yml                column maps, status aliases, solver knobs
  shift_limits.json         per-(shift, role) preferred / acceptable / cap
```

```stdlib http.server web UI.

Routes (JSON unless noted):

    GET  /                          text/html — single-page app
    GET  /static/<path>             the JS module / CSS partial tree

    GET  /api/inputs                which of the 3 files are uploaded,
                                    grouped daily (schedule) / setup (rosters)
    POST /api/inputs/<kind>         upload one (raw .xlsx body)
    DEL  /api/inputs/<kind>         drop one
    GET  /api/server_date           the operating date, per the server's clock
    GET  /api/export.xlsx           download the current results

    GET  /api/dashboard             headline counts + run status
    GET  /api/allocations           every allocation row
    GET  /api/workload              per-staff workload summary
    GET  /api/pairs                 pair map
    GET  /api/warnings              warnings
    GET  /api/unallocated           unallocated flights
    GET  /api/recommendations       Phase-R suggestions per unallocated
    GET  /api/plan                  plan summary payload
    GET  /api/handlers              P2F / NORSE handler picture
    GET  /api/staffing              staffing recommendation
    GET  /api/staff_names           roster names for the drawer dropdowns
    GET  /api/shift_limits          current (shift, role) bands

    GET  /api/overrides             override rows
    POST /api/overrides             append one {values: [...]}
    PUT  /api/overrides/<i>         overwrite row i
    DEL  /api/overrides/<i>         delete row i
    POST /api/overrides/apply_staged   apply staged rows to the data

    POST /api/run                   {date, step} — plan | all | reset | …
    GET  /api/run/status            current run state

Bind: 127.0.0.1:8765. Everything lives in memory for the life of the
process; there is no workbook and no per-date file.
```
