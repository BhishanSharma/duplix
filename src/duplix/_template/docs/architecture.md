# Architecture

How the code is laid out and how a request becomes an allocation.

## The two stages

```
upload files ──► Plan ─────────────────────────► Allocate ──────────────────────► results
                 step1_clean_flights             step3_allocate_flights
                 step2_extract_roster              eligibility matrix
                 plan summary + staffing           CP-SAT solve (solver/)
                                                    post-passes (allocator/)
                                                    force-place leftovers
```

* **Plan** cleans the flight schedule into ops classes (day, night, P2F,
  ferry, charter, test; Gulf flights are extracted but never allocated),
  reads the stored rosters for the date, and builds the summary shown on
  the dashboard.
* **Allocate** builds who-may-take-what (eligibility), solves it with
  CP-SAT, runs the post-passes, guarantees every flight has a person, and
  writes allocations, workload, pairs, warnings and recommendations to the
  in-memory state. The rules are in [allocation.md](allocation.md).

Both run on a background thread (`web/runner.py`); the page polls
`/api/run/status`.

## Code layout

```
src/
  config.py                load + validate configs/config.yml
  schemas.py               the typed row models every stage shares
  state.py                 the in-memory store everything reads and writes

  pipeline/                the operator stages
    plan.py                Plan: step 1 + step 2 + the summary
    step1_clean_flights.py schedule -> cleaned flights per ops class
    step2_extract_roster.py rosters -> who is on which shift on the date
    step3_allocate_flights.py Allocate: eligibility, solve, post-passes, outputs
    recommender_staffing.py people needed per shift pool (dashboard)
    staged_overrides.py    add/remove flight or staff, sick, change role

  allocator/               building blocks used by Allocate
    windows.py             shift hours, STD windows, spacing rules, time helpers
    shift_hours.py         editable shift hours (configs/shift_times.json)
    caps.py                per-(shift, role) workload bands and caps
    eligibility.py         the hard who-may-take-what filters
    p2f_autofill.py        fill missing P2F handlers from the licence column
    p2f_priority.py        reserve P2F flights for their handler first
    greedy_fallback.py     fast heuristic: warm start for CP-SAT, and fallback
    pairings.py            prev-shift / next-shift handover pairs
    pair_validation.py     warnings for odd pairings
    postsolve.py           solver output -> allocation rows, pair labels
    postpass_p2f.py        free time around P2F flights
    postpass_intl.py       free the flight before an INTL departure
    postpass_rebalance.py  level counts within each (shift, role) group
    relievers.py           end-of-shift relievers / early release
    postpass_break.py      a mid-shift break for everyone
    repair.py              place leftovers by chains of moves, no rule relaxed
    force_place.py         last resort: no flight left unallocated
    recommender.py         suggested overrides for problem flights
    invariants.py          independent re-check of the hard rules (tests)

  solver/
    allocator_cpsat.py     the CP-SAT model

  io/
    upload_formats.py      any accepted upload -> openpyxl workbook / .xlsx
    readers.py             flight schedule, rosters, override rows
    roster_library.py      merge the stored rosters for a date
    export.py              the .xlsx download
    export_xml.py          the allocations .xml download

  storage/                 what survives a restart (under data/)
    roster_store.py        uploaded rosters (data/rosters/)
    zc_store.py            Zone Controller list per date (data/zc_lists.json)
    relief_store.py        who was released early, so it rotates

  web/
    __main__.py            the launcher: python -m src.web (start.bat runs it)
    server.py              request loop + serve() (no endpoint logic)
    runner.py              runs Plan / Allocate on a background thread
    core/                  router, responses, static files
    api/                   one module per concern; each registers its routes
    readback/              state -> the JSON the UI renders
    overrides/             writes to config.yml from the Setup sidebar
    static/                index.html, js/ (core, ui, views, charts, panels,
                           sidebar) and css/ (partials listed in styles.css)

configs/                   config.yml, shift_limits.json, shift_times.json
data/                      saved rosters, ZC lists, relief history (git-ignored)
tests/                     pytest suite
docs/                      this documentation
```

## State and storage

`state.STATE` (one `AppState`) holds the uploads, override rows, cleaned
flights, availability and every result. It lives in the server process,
so a restart clears it — except what `storage/` writes under `data/`
(override with `FLIGHT_ALLOC_DATA_DIR`):

| File | Written by |
|---|---|
| `data/rosters/*.xlsx` + `index.json` | roster uploads (Setup) |
| `data/zc_lists.json` | the Zone Controllers panel |
| `data/early_release_log.json` | Allocate's relief pass |

Uploads are converted to `.xlsx` on arrival
([input-formats.md](input-formats.md)), so everything downstream reads one
format.

## HTTP API

Bound to `127.0.0.1:8765`. JSON unless noted.

**Page**

| Route | |
|---|---|
| `GET /`, `GET /index.html` | the single-page app |
| `GET /static/<path>` | JS modules, CSS partials, images |

**Inputs and runs**

| Route | |
|---|---|
| `GET /api/inputs` | which files are uploaded / stored |
| `POST /api/inputs/<kind>` | upload (`sv_portal`, `staff_roster`, `am_roster`); raw file body, `X-Filename` header |
| `DELETE /api/inputs/<kind>` | drop an upload |
| `DELETE /api/inputs/<kind>/<file_id>` | drop one stored roster |
| `GET /api/rosters/coverage?date=` | do the stored rosters cover D and D+1 |
| `POST /api/run` | `{date, step}` — `plan`, `step3` (Allocate), `all` (both), `step1` / `step2` (one half of Plan), `reset`; `pin_prior: true` keeps every placed flight where it is |
| `GET /api/run/status` | state of the current / last run |
| `GET /api/export.xlsx`, `GET /api/export/allocations.xml` | downloads |

**Read-backs** — `GET /api/…`: `server_date`, `dashboard`, `allocations`,
`workload`, `pairs`, `warnings`, `unallocated`, `recommendations`,
`overrides`, `override_types`, `plan`, `handlers`, `staffing`,
`staff_names`, `roster_full`, `sv_portal/columns`, `shift_limits`,
`shift_times`, `hard_caps`, `break_length`, `intl_airports`,
`extraction_filters`, `zc?date=`.

**Changes**

| Route | |
|---|---|
| `POST /api/overrides`, `PUT /api/overrides/<i>`, `DELETE /api/overrides/<i>` | override rows |
| `POST /api/overrides/apply_staged`, `POST /api/staged_form/<op>` | staged add/remove flight or staff |
| `POST /api/recommender/apply`, `POST /api/recommender/apply_all` | turn suggestions into override rows |
| `POST /api/zc/add`, `POST /api/zc/remove` | Zone Controllers for a date |
| `POST /api/shift_limits`, `POST /api/hard_caps` | workload bands / caps |
| `POST /api/shift_times`, `POST /api/shift_times/reset` | shift hours |
| `POST /api/break_length` | break length |
| `POST /api/intl_airports`, `POST /api/intl_airports/remove` | international airport codes |
| `POST /api/extraction_filters/add`, `…/remove` | flights to extract instead of allocate |
