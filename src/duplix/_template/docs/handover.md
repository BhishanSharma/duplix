# Handover

For whoever runs and maintains this after us. Setup and the daily flow
are in the [README](../README.md); the rules are in
[allocation.md](allocation.md).

## State at handover (October 2026)

Checked on a fresh clone with the real 23 and 26 September 2026 files:
installs from `requirements.txt`, 185 tests pass, and a full day
(2,117 flights) is allocated with **0 unallocated**, every shift/role
group within 0–2 flights of each other, and 4–5 squeezed flights.

| | |
|---|---|
| Runs on | Windows (`start.bat`), Python 3.11–3.14; Mac/Linux by hand |
| Allocate time | ~20 s on the development PC, ~40 s on a fresh Python 3.14 install |
| Users | one machine, one browser at a time, no login (binds to `127.0.0.1`) |
| Kept on disk | rosters, ZC lists, early-release history under `data/` |
| Lost on restart | today's schedule, override rows, results — export first |

## What needs attention, and when

| When | What | Where |
|---|---|---|
| Each roster period | Upload the new staff roster and AM list | Setup sidebar |
| Each day | Upload the flight schedule, pick Zone Controllers, check P2F handlers | Dashboard |
| New or dropped international route | Add / remove the airport code | Setup → international airports (`io.sv_portal.international_airport_codes`) |
| Shift hours change | Edit the hours | Setup → shift times (`configs/shift_times.json`) |
| Workload targets change | Preferred / acceptable / hard cap per shift and role | Setup (`configs/shift_limits.json`) |
| SV export renames a column | Add the new name | `io.sv_portal.columns` in `configs/config.yml` |
| New year | Nothing — roster years are worked out from the weekday and the file's date. Only if a roster reads wrong, force one with `allocation: {cycle_year: 2027}` | `configs/config.yml` |
| Before changing anything | Back up `data/` (or set `FLIGHT_ALLOC_DATA_DIR` to a backed-up folder) | |

## Known limits

* **Squeezed flights (W230).** On a busy day 4–5 flights are placed with
  less than the 15 / 30-minute gap. They fall in 15-minute slots where
  more flights depart than people can legally take them (e.g. 21:15 and
  23:20). The Staffing cards show these slots; the fix is people on shift
  then, not engine settings.
* **P2F handlers carry fewer flights** (e.g. 15 and 3 against targets of
  22 and 19): the 3-hour block and the extra time freed around each P2F
  flight are business rules (`p2f_adjustment` in config, `postpass_p2f.py`).
  Shortening them was tested and does not reduce squeezed flights.
* **M-shift P2F before 07:30 (W220).** The 3-hours-before briefing falls
  before the M shift starts; the previous night's N shift has to hand it
  over by hand.
* **Sick call / re-run with pins.** Keeps everyone else in place but adds
  a few squeezed flights and moves ~50 flights through the rebalance,
  relief and break passes. Reset + Allocate gives a cleaner plan but most
  flights change hands.
* **Staffing advice** treats a 15-minute slot above 85% as full. That
  figure was calibrated on two real days; revisit it if the advice and
  the squeezed flights stop matching.
* **Plan summary text** (the plain-text block) still has a volume-only
  "Day staff needed" line; the Staffing cards are the reliable view.
* **GVA** is in the international list but appears in neither real
  schedule; confirm it is a real route.

## Open decisions (business, not code)

* Whether to free less time around P2F flights so handlers carry a fair
  share.
* Whether the 4–5 squeezed flights a day are acceptable, or to roster
  extra people over the evening peak (21:00–23:30).

## Changing the code

* Tests: `pip install -r requirements-dev.txt`, then `python -m pytest`.
  Keep them green; `tests/test_invariants.py` re-checks the hard rules
  independently of the engine.
* Before shipping a rule or performance change, run a real day end to end
  and compare the export: unallocated (must be 0), W230 count, the
  per-group spread on the Workload sheet, and Allocate time.
* Every past change and its reason is in [history.md](history.md).
* Git: work on a branch, fast-forward `main`, delete the branch.
