# Review notes

## Findings not changed (behaviour decisions for the owner)
1. **No auto-retry.** An old `config.yml` comment described a 3-attempt retry
   in `web/runner.py`; it does not exist. Each Allocate is one solve.
2. **Solver budget is 350s**, not 700s. The comment now matches the value.
3. **N shift has a 30-min tail.** `SHIFT_TAIL_END_MIN` applies the uniform
   handover window to N, contradicting comments saying "N has no tail-ext".
   Harmless in postsolve today; pinned by a test so a change is deliberate.
4. **Post-passes run after the solver** and can unbalance workloads it just
   equalised. Moving P2F/INTL removals into the model would fix that but
   needs solver runs to validate.
5. **`step3_allocate_flights.run()` is ~1,200 lines.** Splitting it should be
   done with characterization tests built from real past days.
6. **Exception handling (corrected):** an earlier note overstated this.
   Of ~22 sites, nearly all are narrow (`suppress(ValueError)`) or sit at API
   / advisory boundaries that already surface or log the error. The one
   silent site (`web/readback/session.py`, unreadable SV portal headers) now
   logs its reason.

## P2F-first priority (2026-09-22)
The nominated P2F handler now gets his P2F flights before any normal flight.
* `allocator/p2f_priority.py::select_p2f_priority` picks the P2F flights that
  can be reserved safely (one eligible handler, H17 limit of 8, handler's H16
  cap, H10 spacing between them, no clash with re-solve pins).
* `solver/allocator_cpsat.py` fixes those assignments (`x == 1`) and solves the
  normal flights around them. P2F flights that could not be reserved cost
  `P2F_UNASSIGNED_PENALTY` (1,000,000) when left unallocated vs 100,000 for a
  normal flight. Each run prints a `P2F-first:` line listing anything skipped.
* `allocator/greedy_fallback.py` (moved here from `src/`, where `step3`'s
  import could not find it) now places every P2F flight before normal ones.
* Not covered: the P2F post-pass still removes surrounding flights afterwards.

## Added
- `tests/` (42 tests), `src/allocator/invariants.py`, `requirements-dev.txt`,
  pytest config, README "Tests" section, comment/docstring corrections.

## Map of `step3_allocate_flights.run()` (for the future split)
Line numbers are approximate. Suggested stage functions, in order:

| Lines | Stage |
|---|---|
| 400-612 | read inputs, per-staff and Phase R override sets, D+1 split |
| 612-630 | pre-solve capacity check (W210) |
| 630-967 | build eligibility context (P2F / NORSE handlers, staff today) |
| 967-1054 | eligibility matrix, zero-eligibility drop (W201), pair generation (W211) |
| 1054-1139 | day-level workload target, sick-call pinning |
| 1139-1182 | solve |
| 1182-1419 | assemble rows, P2F then INTL post-pass |
| 1419-1583 | unallocated rows, recommender sidecar |
| 1583-end | write outputs to state |

Do the split behind characterization tests (real past day: inputs plus the
accepted output, compared via `allocator/invariants.py` and row diffs).
