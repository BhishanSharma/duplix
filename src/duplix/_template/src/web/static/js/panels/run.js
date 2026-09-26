/* Plan / Allocate / Reset, and the status pill that tracks them.
 *
 * A run happens on a server worker thread; the POST returns as soon as
 * it's accepted and this polls /api/run/status until it settles. When
 * it does, every held payload is dropped and the visible tab reloads.
 */

import { $ } from "../core/dom.js";
import { get, post } from "../core/api.js";
import * as store from "../core/store.js";
import * as tabs from "../ui/tabs.js";
import { confirmModal } from "../ui/confirm.js";
import * as plan from "./plan.js";
import * as handlers from "./handlers.js";
import * as staffing from "./staffing.js";
import * as flightTrend from "../charts/flight-trend.js";

/** How often to re-check a run in progress. */
const POLL_MS = 700;

let pollTimer = null;
let runStart = null;
let mode = "allocate";        // or "plan"

export function setRunStatus(text, kind /* "busy" | "ok" | "err" | "" */) {
  const el = $("#run-status-pill");
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
  el.className = "run-pill" + (kind ? ` run-pill-${kind}` : "");
}

function setButtonsBusy(busy) {
  $("#plan-btn").disabled = busy;
  $("#reset-btn").disabled = busy;
  if (busy) {
    // While a run is in flight, Allocate is off regardless of P2F
    // readiness — re-derive that once the run settles (see below),
    // rather than force-enabling it here.
    $("#run-btn").disabled = true;
  } else {
    handlers.applyAllocateGate();
  }
}

/** Everything derived from a solve. Dropped whenever the underlying
 *  result could have changed: a run finished, a Reset, a date change. */
function dropDerivedCaches() {
  store.invalidateAll();
  flightTrend.clear();
}

async function startRun(step, label) {
  const date = $("#run-date").value || null;
  if (!date) {
    alert("Pick the allocation date first.");
    return false;
  }
  try {
    await post("/api/run", { date, step });
  } catch (e) {
    setRunStatus(e.message, "err");
    alert(`${label} failed to start: ${e.message}`);
    return false;
  }
  setButtonsBusy(true);
  setRunStatus(`${label} — running…`, "busy");
  runStart = Date.now();
  poll();
  return true;
}

export async function triggerRun() {
  mode = "allocate";
  await startRun("all", "Allocate");
}

export async function triggerPlan() {
  // A new Plan hides the handler panel until the next readback; held
  // payloads go so the tables can't flash last run's numbers while the
  // new one is in flight.
  plan.setPostPlanUI(false);
  dropDerivedCaches();
  mode = "plan";
  await startRun("plan", "Plan");
}

export async function triggerReset() {
  const ok = await confirmModal(
    "Reset results",
    "Clears the allocation, workload summary, pairs, unallocated list "
    + "and warnings from this session. Your uploaded files and override "
    + "rows are kept, so you can go straight back to Plan / Allocate.",
    "Reset",
  );
  if (!ok) return;
  try {
    await post("/api/run", { step: "reset" });
  } catch (e) {
    alert(`Reset failed: ${e.message}`);
    return;
  }
  plan.setPostPlanUI(false);
  dropDerivedCaches();
  setButtonsBusy(false);
  setRunStatus("Reset — results cleared", "ok");
  await tabs.loadTab("dashboard");
  await plan.load();
}

function startPoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, POLL_MS);
}

function stopPoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

/** Check the run's state; on completion, refresh what's on screen.
 *  Also called once at page load, to pick up a run already in flight. */
export async function poll() {
  let s;
  try {
    s = await get("/api/run/status");
  } catch {
    return;      // transient — the next tick picks it up
  }
  const elapsed = s.elapsed_s != null
    ? s.elapsed_s
    : runStart ? Math.round((Date.now() - runStart) / 1000) : 0;
  const label = mode === "plan" ? "Plan" : "Allocate";

  if (s.state === "running") {
    setButtonsBusy(true);
    setRunStatus(`${label} — running… ${elapsed}s`, "busy");
    startPoll();
    return;
  }
  stopPoll();
  setButtonsBusy(false);

  if (s.state === "failed") {
    setRunStatus(s.error || "Run failed", "err");
    return;
  }
  if (s.state !== "ok") {
    setRunStatus("");
    return;
  }

  setRunStatus(`${label} completed in ${elapsed}s`, "ok");
  dropDerivedCaches();

  // Order matters: the plan readback unhides the handler panel, handlers
  // populates it, then the tab renders.
  await plan.load();
  await handlers.load();
  await staffing.load();

  if (mode === "plan") {
    await tabs.switchTab("dashboard");
  } else {
    await tabs.loadTab(tabs.activeTab());
  }
}

function closeExportMenu() {
  const list = $("#export-menu-list");
  const btn = $("#export-btn");
  if (!list || list.hidden) return;
  list.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  document.removeEventListener("click", onDocClickCloseExportMenu);
}

function onDocClickCloseExportMenu(e) {
  if (!$(".export-menu")?.contains(e.target)) closeExportMenu();
}

function toggleExportMenu() {
  const list = $("#export-menu-list");
  const btn = $("#export-btn");
  if (!list) return;
  const opening = list.hidden;
  list.hidden = !opening;
  btn.setAttribute("aria-expanded", String(opening));
  if (opening) {
    // Defer so this same click doesn't immediately close it via the
    // document listener below.
    setTimeout(() => document.addEventListener("click", onDocClickCloseExportMenu), 0);
  }
}

export function init() {
  $("#run-btn").addEventListener("click", triggerRun);
  $("#plan-btn").addEventListener("click", triggerPlan);

  $("#export-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleExportMenu();
  });
  $("#export-xlsx-btn").addEventListener("click", () => {
    closeExportMenu();
    // Plain navigation: the server sets Content-Disposition, so the
    // browser handles the save dialog.
    window.location.href = "/api/export.xlsx";
  });
  $("#export-xml-btn").addEventListener("click", () => {
    closeExportMenu();
    window.location.href = "/api/export/allocations.xml";
  });
}
