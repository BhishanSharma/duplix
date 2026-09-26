# P2F Handler Validation Gate — Flow Diagram

## Overall Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         DASHBOARD UI                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  [Plan] [Allocate] buttons                                       │
│  ↑         ↑                                                      │
│  │         └──→ DISABLED by handlers.isReady() ✗                 │
│  │             when P2F issues exist                             │
│  │                                                                │
│  └──→ Triggers /api/run (step="plan")                            │
│       Calls read_handlers() → /api/handlers                      │
│                                                                   │
├─────────────────────────────────────────────────────────────────┤
│                    HANDLERS PANEL (display)                      │
├─────────────────────────────────────────────────────────────────┤
│  "Required today:"                                               │
│  ┌─────────────────────────────────────────────────┐            │
│  │ M: 1 of 1 nominated                 [✓ ok]      │            │
│  │ A: 0 of 1 needed — needs 1 more     [⚠ missing] │            │
│  │ N: 1 of 1 nominated                 [✓ ok]      │            │
│  └─────────────────────────────────────────────────┘            │
│                                                                   │
│  Issues (if any):                                                │
│  ┌─────────────────────────────────────────────────┐            │
│  │ ⚠ ABHISHEK ARORA on A, but roster says A1      │            │
│  │ ⚠ ADITHYA ANIL on M, but roster says A         │            │
│  └─────────────────────────────────────────────────┘            │
│                                                                   │
│  Sources: read_handlers() response from backend                 │
│                                                                   │
├─────────────────────────────────────────────────────────────────┤
│               HANDLER ASSIGN PANEL (edit)                        │
├─────────────────────────────────────────────────────────────────┤
│  User searches "ADITHYA ANIL" → sees "on A"                     │
│  Selects "P2F handler — A" → clicks Apply                       │
│                                                                   │
│  POST /api/staged_form/p2f                                       │
│  ↓                                                                │
│  Override row written to state.overrides                         │
│  ↓                                                                │
│  Calls handlers.render() → recomputes handlers.js               │
│  ↓                                                                │
│  Handlers panel updates instantly (no page reload)               │
│  Issues list clears                                              │
│  ↓                                                                │
│  handlers.applyAllocateGate() → checks isReady()                │
│  ↓                                                                │
│  If ready, Allocate button turns BLUE (enabled)                  │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Backend Validation Flow

```
User clicks [Plan] or [Allocate]
         ↓
  POST /api/run (step="plan" or step="all", date="2026-09-26")
         ↓
  src/web/api/runs.py: run()
         ├─→ Checks missing inputs (flights, rosters) ✓
         │
         └─→ If step="all" OR step="step3" AND date == STATE.run_date:
             │
             └─→ read_handlers(STATE) called
                 ↓
         src/web/readback/planning.py
         ├─→ Reads state.cleaned (P2F flights from Plan)
         ├─→ Reads state.availability (today's roster)
         ├─→ Reads state.overrides (user's P2F nominations)
         │
         ├─→ For each P2F flight:
         │   └─→ Which shift window (M/A/N) does it fall in?
         │       └─→ Count per shift
         │
         ├─→ For each override nomination:
         │   ├─→ Does this person exist in today's roster?
         │   ├─→ Are they on the nominated shift?
         │   └─→ If NO → add issue, skip this nomination
         │
         ├─→ For each shift with P2F flights:
         │   ├─→ Compute required = ceil(flight_count / 8)
         │   ├─→ Count valid nominations
         │   └─→ If valid_count >= required → status="ok"
         │       else → status="missing"
         │
         └─→ Return {
                "ready": all shifts in ("ok" or "not_needed"),
                "required_by_shift": {...},
                "shift_status": {"M": "ok", "A": "missing", ...},
                "issues": ["issue1", "issue2", ...],
                ...
             }
                     ↓
         If ready == False:
         │
         └─→ Return HTTP 409 Conflict
             {
               "error": "P2F handler nomination missing...",
               "missing_p2f_shifts": ["A"],
               "issues": [...]
             }
             
         Allocate FAILS, assigner gets clear message

         If ready == True (or no P2F flights):
         │
         └─→ Proceed with Plan or Allocate (HTTP 200)
```

---

## Frontend Gate Logic

### `handlers.js`

```javascript
export function isReady() {
  const h = store.handlers.data;
  // If Plan hasn't run yet, or no P2F data, return true (no gate)
  return !h || h.ready !== false;
  //              ^^^^^^^^^^^^^^
  //              explicitly checking ready field
}

export function applyAllocateGate() {
  const btn = $("#run-btn");  // The Allocate button
  const ready = isReady();
  
  btn.disabled = !ready;      // Disable if NOT ready
  btn.title = ready
    ? ""
    : "Nominate a valid P2F handler for every shift listed below..."
}
```

### `run.js`

```javascript
function setButtonsBusy(busy) {
  if (busy) {
    // During a run, disable everything including Allocate
    $("#run-btn").disabled = true;
  } else {
    // After a run, re-derive Allocate's state based on P2F readiness
    handlers.applyAllocateGate();  // NOT just re-enable
  }
}
```

### Flow on Plan completion

```
Plan finishes (status → "ok")
         ↓
poll() in run.js
         ↓
dropDerivedCaches() — clears old /api/handlers
         ↓
plan.load()        — fetches new Plan summary (calls read_plan_summary)
handlers.load()    — fetches new Handlers (calls read_handlers) ← NEW
         ↓
handlers.render()  — uses new .data to draw cards + issues list
         ↓
handlers.applyAllocateGate()
         ├─→ handlers.isReady()
         ├─→ If false: btn.disabled = true, btn.title = "Fix P2F issues..."
         └─→ If true:  btn.disabled = false, btn.title = ""
```

---

## State Flow Example (User's Data)

### Initial State (After Upload + Plan)

```
Roster:
  ID    | Name                | Shift | Current_Shift
  25316 | GAYATHRI .          | M     | M        (P2F licensed)
  68238 | ABHISHEK ARORA      | A1    | A1
  84096 | ADITHYA ANIL        | A     | A
  96655 | Harshvardhan Thakur | N     | N

Overrides (User entered wrong shifts):
  type  | employee        | shift
  p2f   | ABHISHEK ARORA  | A      ✗ (they're on A1)
  p2f   | ADITHYA ANIL    | M      ✗ (they're on A)

P2F Flights:
  STD   | Shift
  07:30 | M      (1 flight)
  13:05 | A      (2 flights)
  22:10 | N      (1 flight)
```

### read_handlers() Result

```
p2f_flight_count_by_shift = {M: 1, A: 2, N: 1}
required_by_shift = {M: 1, A: 1, N: 1}  // ceil(count/8)

valid_nominees:
  M: GAYATHRI . (from roster)           → count=1 ✓
  A: (none)                             → count=0 ✗
  N: (none from overrides)              → count=0 ✗

issues = [
  "ABHISHEK ARORA on A, but A1 in roster",
  "ADITHYA ANIL on M, but A in roster"
]

shift_status = {
  M: "ok"      (1 required, 1 valid)
  A: "missing" (1 required, 0 valid)
  N: "missing" (1 required, 0 valid)
}

ready = False
        └─ because A and N are "missing"
```

### Handlers Card Display

```
M: 1 of 1 nominated ✓
A: 0 of 1 needed — needs 1 more ⚠
N: 0 of 1 needed — needs 1 more ⚠

Issues:
  ⚠ ABHISHEK ARORA on A, but A1 in roster
  ⚠ ADITHYA ANIL on M, but A in roster
```

### Allocate Button State

```
ready = False
  ↓
$("#run-btn").disabled = true   ← GREY
$("#run-btn").title = "Nominate a valid P2F handler for shifts A, N listed below..."
```

---

### After User Fixes (in Handler Assign)

```
User nominates ADITHYA ANIL for "P2F — A"
         ↓
Override row added: {type: "p2f", employee: "ADITHYA ANIL", shift: "A"}
         ↓
POST /api/staged_form/p2f succeeds
         ↓
handlers.render() called immediately
         ↓
handlers.load() fetches fresh /api/handlers
         ↓
read_handlers() re-validates against NEW overrides:

valid_nominees = {
  M: GAYATHRI . (roster)         → count=1 ✓
  A: ADITHYA ANIL (override)     → count=1 ✓  (they ARE on A in roster)
  N: (none)                      → count=0 ✗
}

issues = [
  "ABHISHEK ARORA on A, but A1 in roster"
  ← (still there, not yet fixed)
]

shift_status = {
  M: "ok"      (1/1)
  A: "ok"      (1/1)  ← CHANGED
  N: "missing" (0/1)
}

ready = False
        └─ because N is still "missing"
```

### But Allocate Still Disabled (Because N Missing)

```
handlers.applyAllocateGate()
  ↓
isReady() → ready = false
  ↓
$("#run-btn").disabled = true   ← still GREY
$("#run-btn").title = "...for shifts N listed below..."
```

---

### After User Also Fixes N

```
User nominates someone on N shift for "P2F — N"
         ↓
Handlers card re-renders → N now shows "1 of 1 nominated ✓"
         ↓
no issues left (or issues don't include N anymore)
         ↓
shift_status = {
  M: "ok",
  A: "ok",
  N: "ok"
}

ready = True
```

### NOW Allocate Button Enables

```
handlers.applyAllocateGate()
  ↓
isReady() → ready = True
  ↓
$("#run-btn").disabled = false  ← BLUE
$("#run-btn").title = ""
```

User clicks [Allocate] → solver runs → no W212 surprises.

---

## Key Decision Points

| State | Allocate Disabled? | Why | What to Do |
|-------|-------|---|---|
| Before Plan runs | Yes | `plan_has_run=false`, no data yet | Click Plan first |
| After Plan, all P2F shifts ready | No | `ready=true` | Click Allocate |
| After Plan, some shifts missing | Yes | `ready=false` | Fix nominations in Handler Assign |
| During a run (any step) | Yes | `setButtonsBusy(true)` | Wait for run to finish |
| Stale/mistyped override | Yes | Issue in list + `shift_status="missing"` | Handler Assign shows the person + issue, re-nominate them |
| Override for someone not on roster | Yes | Issue: "not assignable", `ready=false` | Handler Assign only shows people on today's roster |

