# P2F Handler Validation & Allocate Gate — Implementation Guide

## Problem Solved

When running the allocator, a user (assigner) could enter stale or mistyped P2F handler nominations in the Override drawer, which would silently fail once Allocate ran, leaving flights unallocated with cryptic W212 warnings. The assigner had no visibility into *how many* P2F handlers were actually needed, or whether their nominations matched today's roster.

**Solution:** After Plan runs, show exactly what's required per shift, validate each nomination against today's roster in real-time, surface specific issues, and **disable the Allocate button** until all P2F shifts are properly covered.

---

## Files Modified

### Backend (Python)

#### 1. `src/web/readback/planning.py` — Enhanced `read_handlers()`

**What changed:**
- Computes per-shift P2F requirement: `ceil(p2f_flight_count / 8)` from the cleaned schedule
- Validates *every* override nomination against the roster's actual shift for that person
- Returns detailed payload including:
  - `required_by_shift`: {M: n, A: n, N: n}
  - `valid_count_by_shift`: how many valid nominees per shift
  - `shift_status`: "ok", "missing", or "not_needed" per shift
  - `issues`: list of plain-English problems (e.g., "Alice listed for M but on A1 today")
  - `ready`: True only if every shift with P2F flights has enough valid nominees
  - `plan_has_run`: True after Plan completes

**Why:**
- Makes validation visible immediately after Plan, not later when Allocate runs
- Catches the exact bugs from the user's data (name mismatches, shift mismatches)
- Puts the needed count in front of the assigner so they know what to fix

#### 2. `src/web/api/runs.py` — Allocate Gate

**What changed:**
- Added server-side guard in `/api/run` endpoint (POST)
- When `step="all"` or `step="step3"` and same date as current Plan, checks `read_handlers()` 
- Returns HTTP 409 (conflict) with error message + `missing_p2f_shifts` + `issues` if P2F isn't ready
- Only blocks if Plan has actually run for this date; doesn't block the Plan phase itself

**Why:**
- Belt-and-suspenders: client-side Allocate disable is the UX, but this prevents direct API calls or stale tabs from bypassing it
- Clear error message tells assigner exactly what to fix

---

### Frontend (JavaScript)

#### 3. `src/web/static/js/panels/handlers.js` — Handler Display & Gate

**What changed:**
- Rewrote card display: shows "X of Y nominated" per shift with required count
- Added `.handler-issues` section displaying validation issues inline
- Exported `isReady()` (True if P2F is ready or Plan hasn't run yet)
- Exported `applyAllocateGate()` — sets `#run-btn.disabled` based on readiness and updates tooltip

**Why:**
- Assigner sees exactly what's needed and which nominations failed validation
- Issues list is specific (e.g., "ABHISHEK ARORA listed for A, but on A1 today") not a code
- Single source of truth for whether Allocate should be clickable

#### 4. `src/web/static/js/panels/run.js` — Button State Management

**What changed:**
- Modified `setButtonsBusy()` to call `handlers.applyAllocateGate()` after runs finish, not force-enable
- Allocate stays disabled while any run is in flight (Plan or Allocate itself)
- After a run settles, Allocate's disabled state reflects P2F readiness

**Why:**
- Without this, Allocate would get unconditionally re-enabled every time Allocate polling finished, clobbering the P2F gate
- Now the gate is re-derived each time so it's always in sync with the current handlers state

#### 5. `src/web/static/js/panels/handler-assign.js` — Nomination Application

**What changed:**
- Added import of `handlers.js` panel module
- After applying a nomination via the "Handler Assign" drawer, calls `handlers.render()` to refresh cards + gate immediately

**Why:**
- When an assigner fixes a nomination, they see the gate un-grey instantly without page reload
- Provides immediate feedback that the fix worked

---

### Styling

#### 6. `src/web/static/css/components/modal.css` — Issue List Styling

**What added:**
```css
.handler-issues {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.handler-issues li {
  font-size: 12.5px;
  line-height: 1.4;
  color: var(--warn);          /* #c08400 — orange */
  background: var(--bg-elev);  /* #fafbfc — light gray */
  border-left: 3px solid var(--warn);
  padding: 6px 10px;
  border-radius: 3px;
}
```

**Why:**
- Issues stand out visually without being as loud as an error modal
- Consistent with existing `.handler-card` styling

---

## User Workflow

### Before (Old)
1. Click **Plan** → see some handler info, but no requirements or validation
2. Enter override nominations in Handler Assign drawer (guess what's needed)
3. Click **Allocate** → solver runs, W212 warnings appear post-solve, flights are unallocated
4. Guess which overrides were wrong, go back to Handler Assign, guess again

### After (New)
1. Click **Plan** → **Handlers** card shows:
   - "M: 1 of 1 nominated ✓"
   - "A: 0 of 1 needed — needs 1 more ⚠️"
   - "N: 1 of 1 nominated ✓"
   - **Issues:** "ABHISHEK ARORA listed for A, but on A1 today. Fix the override..."
2. **Allocate button is grey** with tooltip: "Nominate a valid P2F handler for shifts A listed below..."
3. Open Handler Assign drawer, search "ADITHYA ANIL" (who's actually on A today), set to "P2F — A", click Apply
4. Handlers card updates instantly: A now shows "1 of 1 nominated ✓", issues list clears
5. **Allocate button turns blue** (enabled)
6. Click **Allocate** → proceeds, no W212 surprises

---

## Testing Checklist

### Unit Testing (Completed)
- ✅ Synthetic roster/overrides reproducing exact user data scenario
- ✅ `ready: false` with correct `issues` list when overrides are stale/mistyped
- ✅ `ready: true` once nominations are corrected

### Smoke Testing (Recommended on your data)
- [ ] Upload real flight schedule & rosters
- [ ] Click **Plan**
- [ ] Verify Handlers card appears with per-shift counts and issues
- [ ] Verify **Allocate button is disabled** (grey) + tooltip visible
- [ ] Fix a nomination in Handler Assign
- [ ] Confirm Handlers card updates *immediately* without page reload
- [ ] Confirm Allocate button turns **blue** (enabled) automatically
- [ ] Click **Allocate** → should proceed without W212 errors
- [ ] Verify workflow for multiple missing shifts, multiple issues

### Edge Cases
- [ ] Plan with no P2F flights → Handlers block doesn't show (correct)
- [ ] Plan with P2F flights but all shifts covered → Allocate enabled immediately
- [ ] Enter a P2F override for someone not on the roster at all → issue appears
- [ ] Stale browser tab + click Allocate after someone else plans → server 409 response + clear error

---

## Deployment Steps

1. **Backup current code** (if in production)
   ```bash
   git stash
   ```

2. **Apply changes:**
   - Copy modified files into your repo (or apply git patch)
   - Files changed:
     - `src/web/readback/planning.py`
     - `src/web/api/runs.py`
     - `src/web/static/js/panels/handlers.js`
     - `src/web/static/js/panels/run.js`
     - `src/web/static/js/panels/handler-assign.js`
     - `src/web/static/css/components/modal.css`

3. **No schema/config changes** — everything is backward-compatible; existing overrides/roster formats work as-is

4. **Test with your data:**
   ```bash
   python -m src.web  # or however you start the server
   ```

5. **Verify in browser:**
   - Load http://localhost:8000 (or your port)
   - Follow the smoke testing checklist above

---

## Known Limitations

- **M1/A1 P2F flights:** These shifts don't nominate handlers (per user direction 2026-05-10). If a P2F flight's STD falls in M1 or A1 window, it must also fall in M or A to be allocated to a handler; otherwise it lands unallocated. This is expected behavior, not a bug.
- **Multiple handlers per shift:** The UI and backend support nominating multiple handlers per shift (for >8 P2F flights), but the current Handler Assign drawer presents a single-select per shift. This can still be done by entering multiple `type=p2f` override rows with the same shift, each with a different employee.
- **License auto-grant:** If a nomination is made for someone without a P2F license on the roster, the solver auto-grants it and emits a W212 WARN (not ERROR). The nomination is still considered valid for the gate.

---

## Support & Questions

If the gate rejects an Allocate but you believe the nominations are correct:

1. **Check the exact error message** — it's in the HTTP 409 response
2. **Verify each person's shift** — open the roster file and confirm the `current_shift` column
3. **Check for name case/spaces** — "Harshvardhan" vs "Harshvardhan Thakur" are different people
4. **Run Plan again** — readbacks are computed fresh each time

If issues persist, enable browser DevTools (F12 → Network tab) and share the `/api/handlers` response JSON.

