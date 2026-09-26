# Quick Start: P2F Handler Validation Gate

## What This Does

After you click **Plan**, the dashboard now shows:

1. **Handlers card** displays per-shift requirements:
   ```
   M: 1 of 1 nominated ✓
   A: 0 of 1 needed — needs 1 more ⚠
   N: 1 of 1 nominated ✓
   ```

2. **Issues list** (if any) flags invalid nominations:
   ```
   ⚠ ABHISHEK ARORA listed for A, but on A1 today. Fix or nominate someone else.
   ⚠ ADITHYA ANIL listed for M, but on A today. Fix or nominate someone else.
   ```

3. **Allocate button is grey** (disabled) with tooltip explaining what to fix

4. **Fix the issues** by clicking in the Handler Assign panel, re-nominating the correct people

5. **Handlers card updates instantly** (no page reload), Allocate button turns **blue**

6. Click **Allocate** → proceeds without W212 surprises

---

## For Users: Day-to-Day

### Setup (One-time)

No setup needed. Existing override rows are automatically validated when Plan runs.

### Daily Workflow

```
1. Upload flight schedule and rosters
2. Pick the allocation date
3. Click [Plan]
   → Handlers card shows what's needed per shift
   → Any stale/mistyped nominations flagged in orange
   → Allocate button is grey if issues exist

4. (If Allocate is grey) Fix the nominations:
   - Click in Handler Assign pane
   - Search for a person on the correct shift
   - Set them as P2F handler for the needed shift
   - Click Apply
   
   → Handlers card updates instantly
   → If all shifts now covered, Allocate button turns blue

5. Click [Allocate]
   → Flight allocation runs
   → Results tab shows allocations, workload, etc.
```

---

## For Developers: Integration Checklist

### Step 1: Copy Files
Replace these 6 files in your codebase:

```
src/web/readback/planning.py              (backend validation)
src/web/api/runs.py                       (Allocate gate)
src/web/static/js/panels/handlers.js      (display + button disable)
src/web/static/js/panels/run.js           (button state sync)
src/web/static/js/panels/handler-assign.js (instant update)
src/web/static/css/components/modal.css   (issue list styling)
```

### Step 2: No Migrations Needed
- No database changes
- No config.yml changes
- No schema changes
- Backward-compatible with existing data

### Step 3: Test Locally

**Start the server:**
```bash
python -m src.web
# or your usual startup command
```

**Run smoke test:**
```bash
1. Open http://localhost:8000
2. Upload flights + rosters
3. Click [Plan]
4. Look for Handlers card
5. If you added some "wrong" P2F overrides (diff shifts), you'll see orange issues
6. Fix one override in Handler Assign
7. Watch Handlers card update immediately + Allocate button change color
8. Click [Allocate]
```

### Step 4: Deploy to Production
No special steps—just deploy the files and restart the web service.

---

## API Endpoints (For Integrators)

### GET `/api/handlers`
Returns the current P2F handler state (already existed, now enhanced).

**New fields in response:**
```json
{
  "run_date": "2026-09-26",
  "p2f": [
    {"name": "GAYATHRI .", "shift": "M", "source": "roster"},
    {"name": "HARSHVARDHAN", "shift": "M", "source": "override"}
  ],
  "required_by_shift": {"M": 1, "A": 1, "N": 1},
  "valid_count_by_shift": {"M": 2, "A": 0, "N": 0},
  "p2f_flight_count_by_shift": {"M": 1, "A": 2, "N": 1},
  "shift_status": {"M": "ok", "A": "missing", "N": "ok"},
  "issues": [
    "P2F nomination \"ABHISHEK ARORA\" is listed for shift A, but today's roster has them on A1..."
  ],
  "plan_has_run": true,
  "ready": false
}
```

**Key fields:**
- `ready`: True = Allocate should be enabled; False = show issues and wait for fixes
- `shift_status`: "ok" (enough nominees), "missing" (not enough), "not_needed" (no P2F flights)
- `issues`: List of validation errors in plain English

### POST `/api/run` — Allocate Gate

When calling with `{"step": "all", "date": "2026-09-26"}`:

**If P2F is not ready:**
```
HTTP 409 Conflict
{
  "error": "P2F handler nomination missing or invalid for shift(s): A, N. Fix it in the Override drawer, then Plan again before Allocate.",
  "missing_p2f_shifts": ["A", "N"],
  "issues": [...]
}
```

**If P2F is ready (or no P2F flights):**
```
HTTP 200 OK
{"started": true, "step": "all"}
```

---

## Troubleshooting

### "Allocate button won't turn blue"

1. **Check the issues list** — scroll down in the Handlers card
2. **Each issue is specific** — e.g., "Alice listed for M, but on A today"
3. **Fix each issue:**
   - Open Handler Assign pane
   - Search the person mentioned
   - Confirm their shift (shown in parentheses)
   - If they're on a different shift than the issue claims, you have the wrong person
   - Set them to the needed shift (or nominate someone on the correct shift instead)
   - Click Apply
4. **Handlers card updates immediately** — watch it refresh
5. Once all issues are gone and all shifts show "X of Y nominated", Allocate button turns blue

### "Allocate returned a 409 error"

This means the button was somehow greyed out on the server's side too (shouldn't happen, but is a safety net).

**Fix:**
1. Copy the error message exactly
2. Open the Handlers card and look for matching issues
3. Fix the overrides as above
4. Click [Plan] again
5. Try [Allocate] again

### "One of my P2F nominations disappeared"

Likely cause: the override row was removed or the employee name was edited and no longer matches anyone in the roster.

**Fix:**
1. Go to Handler Assign
2. Search the person's name (exact match)
3. Re-nominate them for the needed shift
4. Watch the Handlers card update

---

## FAQ

**Q: Can I nominate multiple people for one shift?**
A: The UI lets you nominate one per shift. For >8 P2F flights (needing 2+ handlers), you'd add multiple `type=p2f` override rows manually, each with a different employee.

**Q: What if someone doesn't have P2F in their license cell?**
A: The nomination is still valid. The solver will auto-grant the license and log a W212 WARN.

**Q: Do I have to wait for this check every time?**
A: Only after you click [Plan] and only if there are P2F flights that day. If there are no P2F flights, the Handlers card doesn't even show.

**Q: What if the screen is wrong about someone's shift?**
A: The Handlers card reads from the current roster. Check the uploaded roster file — the `current_shift` column — or re-upload it.

**Q: Can I disable this gate?**
A: No, and you shouldn't. But you can open the browser DevTools (F12) and manually set `document.getElementById("run-btn").disabled = false` to force Allocate for testing. (Not recommended in production.)

