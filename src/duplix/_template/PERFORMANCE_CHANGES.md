# Performance pass — 2026-09-26

You asked for faster Allocate runs **without** trading away accuracy. Here's
exactly what changed, why, and what did *not* change.

## What was slow, and why (diagnosis)

- `configs/config.yml` gives CP-SAT a 700-second budget for a cold solve.
  That number is already the result of real tuning history documented in
  the file's own comments (350s caused visible fairness regressions on a
  2,117-flight day).
- `src/step3_allocate_flights.py` runs the solver **up to twice** per
  Allocate click: first a "hard spread ≤ 2" probe (capped at up to 300s),
  and — only if that probe comes back INFEASIBLE/UNKNOWN — a full retry at
  the full 700s with the spread constraint softened. In the worst case
  that's up to ~1000s (~16–17 min) for one Allocate.
- Every cold solve (the first Allocate of the day) started CP-SAT's search
  from nothing, and `num_search_workers` was never set, so it wasn't
  reliably using all available CPU cores for parallel search.

## What changed

1. **Multi-core search is now explicit.**
   `src/solver/allocator_cpsat.py` sets `solver.parameters.num_search_workers`
   via a new `_resolve_num_workers()` helper: auto-detects CPU cores (capped
   at 8, OR-Tools' own sweet spot for portfolio search), or honors an
   explicit override via the new `solver.num_workers` key in
   `configs/config.yml` (0 = auto, default).

2. **Cold solves get a free warm start.**
   When a solve has no (or few) genuine prior-run `solution_hints`, it's now
   seeded with a fast greedy assignment (`allocator/greedy_fallback.py`,
   already in the codebase as the INFEASIBLE safety net — now reused here
   as a *starting point*, not just a fallback). This only affects flights
   the caller didn't already hint or pin. Hints are suggestions, not
   constraints, so this cannot make a result *worse* — it just gives CP-SAT
   a real, valid, non-empty incumbent immediately instead of having to find
   one from scratch, which is normally where a lot of early wall-clock time
   goes.

3. **The "re-solve mode" fast path (200s / 5% gap) is untouched and still
   correctly gated.** It's now keyed on `n_caller_hints` — genuine
   prior-CP-SAT-run hints only — so the new greedy seed from (2) can never
   accidentally trigger the relaxed gap/time cap that's meant for sick-call
   and override re-solves. A first-ever cold solve for the day still runs
   to the **full `max_seconds` budget** and the **full 0%-gap / proven-
   optimal standard** — same acceptance bar as before, just starting from
   a better point.

## What did *not* change (on purpose)

- `max_seconds: 700` — untouched. Shrinking it would trade accuracy for
  speed, which is the opposite of what you asked for.
- No gap tolerance was added to cold solves. The only place that accepts a
  gap is the pre-existing, unmodified re-solve-mode path for genuine
  warm starts.
- No hard constraints, eligibility rules, or objective weights were
  touched.

## What to expect

- The probe+retry double-solve should trigger less often (a good warm
  start makes it more likely the spread≤2 probe finds a feasible answer
  within its own budget), and each individual solve should reach a given
  quality level faster thanks to parallel search.
- Worst case (a day the solver genuinely can't crack quickly) is still
  bounded by the same `max_seconds` config you already had — this is a
  wall-clock optimization, not a change to what the solver is willing to
  call "done."
- I could not install `ortools`/`pydantic` in this sandbox (no network) to
  run a live timing comparison, so I can't hand you a before/after number.
  Please run a real Allocate on your machine and check the `solver:
  status=..., elapsed=...s` line in the console output — that's the number
  to watch. If you want, tell me those numbers and I'll tune further
  (e.g. explicit `num_workers`, or address the probe/retry double-solve
  more directly) on the next pass.
