/* The Override drawer — a slide-out reachable from any tab.
 *
 * It composes four independent sections (rows, staged forms, airport
 * codes, shift limits); this module owns the shell, the open/close
 * behaviour, and the two actions that span sections: Save, and
 * Save & re-run.
 */

import { $ } from "../core/dom.js";
import { post } from "../core/api.js";
import * as store from "../core/store.js";
import { confirmModal } from "../ui/confirm.js";
import * as rows from "./rows.js";
import * as stagedForms from "./staged-forms.js";
import * as airports from "./airports.js";
import * as shiftLimits from "./shift-limits.js";

/** How long the "saved" acknowledgement stays on the Save button. */
const SAVE_ACK_MS = 1500;

export const template = `
  <aside class="drawer" id="override-drawer" hidden>
    <header>
      <h2>Overrides</h2>
      <button class="close-btn" id="close-override" type="button" aria-label="Close">&times;</button>
    </header>
    <div class="drawer-body">
      ${rows.template}
      ${stagedForms.template}
      ${airports.template}
      ${shiftLimits.template}
    </div>
  </aside>`;

let deps = {};

export function isOpen() {
  const el = $("#override-drawer");
  return !!el && !el.hidden;
}

export function close() {
  $("#override-drawer").hidden = true;
}

export async function open() {
  $("#override-drawer").hidden = false;
  await Promise.all([
    rows.load(),
    store.staffNames.load({ force: true }),
    shiftLimits.load(),
    rows.loadTypes(),
  ]);
  // The employee dropdowns render from the roster, so re-render once
  // the names are in.
  rows.render();
}

/** Save every visible row, then materialise the staged add/remove rows
 *  onto the data sheets so the dashboard shows the projected state
 *  BEFORE the assigner hits Allocate. */
async function saveWithoutRerun() {
  const failed = await rows.saveAllRows();
  if (failed > 0) {
    alert(`${failed} override row(s) failed to save. See console.`);
    return;
  }
  const dDay = deps.currentDate();
  if (dDay) {
    try {
      const resp = await post("/api/overrides/apply_staged", { date: dDay });
      const summary = Object.entries(resp.applied || {})
        .filter(([, n]) => n > 0)
        .map(([k, n]) => `${k}: ${n}`)
        .join(", ");
      if (summary) console.log(`[staged] applied ${summary}`);
    } catch (e) {
      console.warn("apply_staged failed", e);
      alert(`Staged overrides could not be applied: ${e.message}`);
      return;
    }
  }
  await rows.load();
  await deps.refreshDashboard();

  const btn = $("#override-save");
  const original = btn.textContent;
  btn.textContent = "Saved & dashboard refreshed ✓";
  setTimeout(() => { btn.textContent = original; }, SAVE_ACK_MS);
}

/** Save every visible row, confirm, then re-solve.
 *
 * The save has to happen first: an earlier version ran the solver
 * without persisting pending edits, so a row typed and then
 * "Save & re-run" was silently lost. */
async function saveAndRerun() {
  const btn = $("#override-run");
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  let failed = 0;
  try {
    failed = await rows.saveAllRows();
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
  if (failed > 0) {
    alert(`${failed} override row(s) failed to save. See console; not running.`);
    return;
  }

  // Say explicitly what the re-solve will and won't move — otherwise
  // the button just saves and runs with no signal that previous
  // allocations survive.
  const dateStr = (deps.currentDate() || "").trim();
  const ok = await confirmModal(
    `Re-allocate${dateStr ? ` for ${dateStr}` : ""}`,
    "Saved. About to re-allocate.\n\n"
    + "✓ Previous allocations are PRESERVED (warm-start hints from prior iter).\n"
    + "✓ Only flights affected by your overrides (sick / removed staff / "
    + "add+remove flight / waivers) will be redistributed.\n"
    + "✓ Typical run time: 1-3 min (vs 8-12 min for a fresh solve).\n\n"
    + "Proceed?",
    "Re-allocate now",
  );
  if (!ok) return;
  close();
  deps.triggerRun();
}

export function init(injected) {
  deps = injected;
  rows.init(injected);
  stagedForms.init(injected);
  airports.init();
  shiftLimits.init();

  $("#close-override").addEventListener("click", close);
  $("#override-save").addEventListener("click", saveWithoutRerun);
  $("#override-run").addEventListener("click", saveAndRerun);
}
