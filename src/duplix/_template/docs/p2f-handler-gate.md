# P2F handler checks before Allocate

A P2F (passenger-to-freighter) flight can only be handled by a nominated
P2F handler of its shift (M, A or N), and one handler takes at most 8 P2F
flights. A missing or wrong nomination would leave P2F flights badly
placed, so the app checks the handlers after **Plan** and keeps
**Allocate** disabled until they are right.

## Where handlers come from

For each shift with P2F flights, in this order:

1. **Roster cells** — `P2F/M`, `P2F/A`, `P2F/N` (or a combined ZC + P2F
   cell) on the date.
2. **Override rows** of type `p2f` (Handler Assign in the drawer). A
   nomination for someone without a P2F licence is accepted for the day
   with a W212 warning.
3. **Licence auto-fill** — if the shift still needs more (one handler per
   8 P2F flights), staff on that shift whose licence column says P2F.

Someone can be taken off P2F for the day with a `p2f_exclude` override
("Not doing P2F today").

## What is checked

After Plan, the **P2F handler** cards show, per shift: how many handlers
are needed, how many valid ones there are, and any problems — e.g. a
nominated name not on today's roster, or rostered on a different shift
(`… listed for A, but on A1 today`).

* The page disables **Allocate** while any shift with P2F flights is short
  (`src/web/static/js/panels/handlers.js`).
* The server enforces the same rule: `POST /api/run` answers 409 with the
  missing shifts and the problems when Plan has run for that date and the
  handlers are not ready (`src/web/api/runs.py`).
* The check is computed in `read_handlers()`
  (`src/web/readback/planning.py`); fixing a nomination refreshes the
  cards and the button immediately.

## During Allocate

* P2F flights that only one handler can take are fixed to that handler
  before the rest is solved.
* A handler takes no normal flight from 2 h before to 1 h after each of
  their P2F flights, and no normal international flight.
* The P2F post-pass then moves up to four more normal flights off the
  shift's handler around each P2F flight.
* An M-shift P2F before 07:30 raises W220: its pre-brief falls before the
  M shift starts, so a handover from the previous night shift is needed.

See [allocation.md](allocation.md) for the full rule set.
