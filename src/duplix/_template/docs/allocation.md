# How allocation works

What **Allocate** does, in order, and the rules it applies. The goal: every
flight gets a handler, and people on the same shift and role get as close
to the same number of flights as the rules allow.

Code: `src/pipeline/step3_allocate_flights.py` orchestrates;
`src/solver/allocator_cpsat.py` is the model; `src/allocator/` holds the
pieces. Times below are minutes from 00:00 of the allocation date (an N
shift flight at 02:00 the next day is 26:00).

## 1. Who is working

From the merged rosters for the date: STAFF and ZC on shift M, M1, A, A1 or
N. AM staff never take flights. Then the overrides: `sick` and
`change_role` to AM take people out; `max_flights` lowers a cap;
`cutoff_time` limits someone to an STD window.

**P2F handlers** per M / A / N shift come from roster cells (`P2F/M`, …),
the `p2f` override rows, and — when a shift still needs more (one per 8 P2F
flights) — staff on that shift whose licence column says P2F.

## 2. Eligibility (hard rules)

A person may take a flight only if all of these hold
(`allocator/eligibility.py`):

| Rule | |
|---|---|
| Shift window | the STD falls in their shift's STD window (`allocator/windows.py`; A and A1 have a short, penalised outer band) |
| P2F flights | only a nominated P2F handler of that shift (M / A / N), P2F-licensed |
| P2F buffer (H4) | a handler takes no normal flight from 2 h before to 1 h after each of their P2F flights |
| Handlers and INTL (H20) | a shift's P2F handler takes no normal international flight |
| INTL coverage | for an international departure, their shift covers STD − 75 min through STD |
| Newbies | no international flights |
| ZC end of shift (H21) | a ZC takes no normal flight in the last 60 min of their shift (`zc_cutoff`) |
| Per-staff window (H11) | inside their `cutoff_time` override, if any |

A flight nobody is eligible for skips the solver (W201) and goes straight
to force-place (step 7).

## 3. The CP-SAT solve

Hard constraints:

* **One handler per flight** — a flight may be left unassigned, but that
  costs 100,000 (1,000,000 for a P2F flight).
* **Spacing (H10)** — one person's flights at least 15 min apart; 30 min
  when a domestic flight is followed by an international one, and between
  two P2F flights. Written as "at most one flight per person in each
  window" constraints.
* **Caps (H16)** — per person, from `configs/shift_limits.json` (lowered
  by `max_flights`). P2F flights count. A `raise_cap` override lets one
  named flight go above it.
* **P2F** — at most 8 per handler (H17); P2F flights that only one handler
  can take are fixed to that handler before anything else.

Soft objective (lower is better):

| Term | Weight | Effect |
|---|---|---|
| Unassigned flight | 100,000 (P2F 1,000,000) | place everything that can be placed |
| A/A1 outer STD band | 5,000 per flight | use only when needed |
| ZC below band minimum | 4,000 per flight short | keep ZCs at their floor (14) |
| Spread in a (shift, role) group | 600 per flight of max − min, +1,000 per flight beyond 1 | equal counts within a group |
| Distance from the day's level | 200 per flight, +300 beyond 2 below, +500 beyond 5 below | everyone near the day's target |
| INTL pair 15–20 / 21–29 min apart | 800 / 100 | more room before INTL departures |
| Handover in a rush band | 200 | prefer someone not starting / ending a shift |
| INTL count spread per shift | 50 | share INTL departures |
| ZC in report buffer | 15 | keep ZC report time free |
| Handover overlap to next shift / awkward-window non-preferred shift | 10 each | tie-breakers |

The **day's level** per (shift, role) is the shift's share of the flights
it can take — each flight split across every shift whose window covers it,
by headcount — clipped to the band in `shift_limits.json`. People held
below that by their own cap, window or few eligible flights are left out
of the spread terms so they don't pull their colleagues down.

**Speed.** The solve starts from a complete greedy allocation
(`allocator/greedy_fallback.py`: chronological, giving each flight to the
person least needed in the next hour, weighed against their load) and runs
for `solver.max_seconds` (15 s) with light presolve. A re-run that has the
previous allocation to start from stops at a 5 % optimality gap. If the
solver returns nothing usable, the greedy allocation is used and W299 is
raised.

## 4. Post-passes, in order

1. **P2F** (`postpass_p2f.py`) — moves up to 4 normal flights off the
   host handler around each P2F flight (after STD − 3 h, − 1 h and STD).
2. **INTL** (`postpass_intl.py`) — moves the domestic flight before each
   international departure off that handler (two when a previous INTL is
   15–20 min earlier).
3. **Rebalance** (`postpass_rebalance.py`) — while a (shift, role) group
   is more than 1 apart, move an eligible flight from the busiest to
   someone at least 2 lower.
4. **Relief** (`relievers.py`) — everyone except N and A1 gets one
   reliever from the next shift (A1 backs up A); people left without one
   are released 60 min early (no flights then), rotating day to day.
5. **Break** (`postpass_break.py`) — a 30-min mid-shift break for
   everyone, made by 1-for-1 swaps only (counts don't change).
6. **Repair** (`repair.py`) — for each flight still without a person,
   look for a short chain of moves that frees someone: A takes the
   flight, A's clashing flight goes to B, B's to C (up to 3 hops). A chain
   is kept only if every step keeps eligibility, spacing, caps, early
   releases and breaks; P2F flights never move, and nothing goes back to
   someone the P2F / INTL passes took it from. On real days this places
   12–14 of ~18 leftovers with no rule relaxed.
7. **Force-place** (`force_place.py`) — every flight still without a
   person goes to whoever breaks the fewest / mildest rules (eligibility,
   then cap, then spacing, then load against their target). Each is
   flagged `FORCED:` and listed as W230.
8. **Rebalance again** — levels the groups once more without moving the
   forced flights or touching early releases and breaks.
9. Pair labels (planned by / relieved by) are recomputed for the final
   allocation.

Every move in passes 1–6 and 8 keeps eligibility, spacing and caps.

## 5. Re-running after a change

With a `sick` or `change_role` override (or `pin_prior` from the
Unallocated tab), every earlier assignment of an unaffected person is
pinned, so only the freed flights are placed. Pins that clash with each
other or with a cap are released rather than making the model infeasible;
force-placed rows are never pinned. Rebalance, relief and break still run,
so a few other flights can move. To start fresh instead: **Reset**,
upload the schedule again, then Plan and Allocate.

## 6. Staffing advice

The dashboard's staffing cards (`pipeline/recommender_staffing.py`) judge
each pool (Morning M+M1, Afternoon A+A1, Night N) on its **busiest
15-minute slots**, not hourly averages. One person can take one flight
per 15 minutes, so for every slot it compares the departures with the
people who could take them (shift STD window; ZCs stop an hour early):

* over 100% — flights must be squeezed: **ADD**, to the shift covering
  that slot;
* over ~85% — tight; on real days squeezing started around 89%;
* **REMOVE** only when every slot the shift covers stays at or below 85%
  without those people, total volume still fits the per-person targets,
  and the last Allocate squeezed nothing in that pool's hours.

Plan computes it; Allocate refreshes it with the flights it had to
squeeze. Total volume alone can suggest spare people on a day whose
peaks have none — the card says so explicitly.

## Tuning

| Setting | Where |
|---|---|
| Solver time budget | `solver.max_seconds` in `configs/config.yml` |
| Bands and caps per shift / role | `configs/shift_limits.json` or Setup |
| Shift hours | Setup (saved to `configs/shift_times.json`) |
| Break length, early-release minutes, ZC cut-off | `break_pass`, `relief_pass`, `zc_cutoff` in `config.yml` |
| Objective weights | constants at the top of `src/solver/allocator_cpsat.py` |
