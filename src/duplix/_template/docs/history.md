# Change history

Notable changes and why they were made, newest first. Commit messages hold
the full detail.

## 2026-10-11 — Handover fixes

* Roster years are worked out (banner, weekday, the file's date, today);
  `allocation.cycle_year: 2026` is gone, so nothing breaks in January.
* `ZRH  # smoke test` removed from the international airport list.
* `start.bat` now checks for Python 3.11–3.14 and says so when it can't
  find one, instead of accepting any Python and failing at install.
* [handover.md](handover.md) added.

## 2026-10-11 — Staffing advice, repair pass, upload and dashboard

* Repair pass before force-place: leftover flights placed through short
  chains of moves, no rule relaxed — squeezed flights 14 → 5 a day.
* Staffing cards judged on the busiest 15-minute slots instead of hourly
  averages; they had advised removing staff the engine needed.
* Upload ~4 s → ~1 s with a spinner and a green button when loaded; Plan
  and Reset appear only when useful; Reset also clears the schedule; the
  workload charts appear only after Allocate.

## 2026-10-11 — Web-only, dead code removed

* One way to start the app: `python -m src.web` (what `start.bat` runs).
  The separate `src/cli.py` launcher is gone; the `flight-alloc` command
  now points at the web launcher.
* Removed code nothing used: 10 old model classes and 5 helpers in
  `schemas.py` (agent / approval queue, the old override-sheet rows),
  old parsing helpers in `readers.py`, the retired INTL shift-edge rule,
  `solve_allocation_hard_only`, the legacy cap views in `caps.py`, and
  unused imports and variables.
* Removed 11 settings no code read: `allocation.default_date`,
  `solver.deficit_penalty_lambda`, the `state:` section, `io.*.sheet`,
  `pax_regex`, `skip_blank_columns`, `sheet_glob`, `status.shifts`,
  `non_shift_recognized`, `unrecognized_treatment`.

## 2026-10-11 — Project tidy-up

* Stage modules moved to `src/pipeline/` and on-disk stores to
  `src/storage/`; the unused duplicate `src/greedy_fallback.py` removed.
  No behaviour change.
* Documentation consolidated under `docs/`; it replaces
  `docs/REVIEW_NOTES.md`, `PERFORMANCE_CHANGES.md` and the
  `P2F_HANDLER_VALIDATION_UPDATES/` hand-off package.

## 2026-10-10 — Flexible input files

* Uploads accept `.xlsx`/`.xlsm`, old `.xls`, CSV/TSV/text and HTML-table
  "xls" exports; each is stored as `.xlsx`.
* Flight schedule: header row found below titles, headers matched ignoring
  case/punctuation plus common relabels, more time and date shapes; a
  missing required column is refused at upload with its name (it used to
  drop every flight silently).
* Rosters: more day-header shapes, day/month order detected, more ID /
  name / licence header names.
* Flight numbers no longer show as `1.0`.

See [input-formats.md](input-formats.md).

## 2026-10-10 — Speed and fairness

Allocate went from ~6 minutes to ~20 s on a 2,100-flight day, while
placing more flights.

* The model is built from per-person indexes (it used to loop over every
  flight × person in each term); spacing is written as per-window
  "at most one" constraints instead of ~3.7 M pair constraints.
* One solve instead of a spread ≤ 2 probe followed by a second full solve.
* Complete warm start from a smarter greedy (look ahead one hour, weigh
  load against each person's target); light presolve; budget 700 s → 15 s.
* A second leveling pass after force-place. On the real 26-Sep day, every
  STAFF group except N is within 1 flight.
* Re-running Allocate no longer duplicates the Warnings list.

## 2026-10-10 — Allocation review fixes

* P2F handlers now get their 2 h / 1 h buffer for P2F flights at
  12:31–14:30 and 21:01–23:00 (those were credited to M1 / A1, which have
  no handlers).
* The day's workload target shares each flight across every shift that can
  take it (M1 / A1 were always targeted at the band floor).
* People held low by their own cap, window or supply no longer drag their
  group's spread down (one capped person used to limit everyone to their
  count + 2 and leave flights unallocated).
* The hard per-person ceiling at level + 1 removed; the cap is the only
  hard ceiling.
* `cutoff_time` for N-shift staff works across midnight.
* Sick / role-change re-runs: force-placed rows are not pinned, clashing
  pins are released instead of failing, and the greedy fallback keeps pins.
* Moved flights go to whoever is furthest below their own target (ZCs and
  N staff used to absorb them all); the row-order tie-break (S4) is off.

## 2026-10-04

* **A1 backup relievers** — A staff without an N reliever are relieved by
  A1 staff of the same role (one each) before anyone is released early.
* **Setup sidebar** sections are collapsible; Roster files opens while a
  roster is missing.

## 2026-10-01

* **Zero unallocated** — whatever cannot be placed within the rules is
  force-placed on whoever breaks the fewest, and listed as W230.
* **Relievers and early release** — one reliever per person from the next
  shift; people without one finish 60 min early, rotating day to day.

## 2026-09-26

* **P2F handler gate** — Allocate is disabled (and the API refuses) until
  every shift with P2F flights has valid handlers. See
  [p2f-handler-gate.md](p2f-handler-gate.md).
* **H20** — a shift's P2F handler takes no normal international flight
  (their own international P2F legs are unaffected).
* **H4 buffer** — no normal flight for a handler from 2 h before to 1 h
  after each of their P2F flights.
* **Spacing** — 15 min strict; 30 min for domestic → international and
  between two P2F flights.

## 2026-09-22 / 23

* **P2F first** — P2F flights that only one handler can take are fixed to
  them before the rest is solved; an unplaced P2F flight costs 10× a normal
  one.
* **Greedy fallback** — if CP-SAT returns nothing, a greedy allocation is
  used (W299) instead of leaving the day empty.
* **ZC floor made soft** — a hard ZC minimum on a day with no night flights
  once made the whole model infeasible.
* **Rebalance and break passes** added after the solver.

## Earlier

Shift windows and spacing rules, the INTL overhaul (DEP-side international,
D − 75 coverage, INTL spacing and fairness terms), the P2F post-pass, the
day-level workload target, sick-call re-runs and the override drawer were
built between May and September 2026.
