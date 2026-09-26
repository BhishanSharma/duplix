/* Nominated handlers — the P2F slot per shift.
 *
 * P2F handlers are auto-picked from STAFF roster cells marked M/P2F,
 * A/P2F, N/P2F, topped up by type=p2f override rows.
 *
 * This is also the Allocate gate: a nomination only counts if the
 * named person is actually on that shift TODAY (the same check the
 * solver's W212 warning makes) — a stale or mistyped override used to
 * look fine here and only fail once Allocate ran. Now /api/handlers
 * validates it up front and Allocate stays disabled until every shift
 * with P2F flights has enough valid handlers.
 */

import { $, escapeHTML } from "../core/dom.js";
import * as store from "../core/store.js";

/** Shifts that get a P2F handler card, in display order. */
const P2F_SHIFTS = ["M", "A", "N"];

export const template = `
  <div class="block" id="handlers-block" hidden>
    <h2>P2F handlers required today — <span id="handlers-date">—</span></h2>
    <p class="subdued">P2F handlers are auto-picked from STAFF roster cells
      marked <code>M/P2F</code>, <code>A/P2F</code>, <code>N/P2F</code>, or
      nominated via the <strong>Handler Assign</strong> panel — one handler
      per 8 P2F flights in a shift. <strong>Allocate stays disabled</strong>
      until every shift below is covered.</p>
    <div class="stat-grid" id="handlers-grid"></div>
    <div id="handlers-issues"></div>
  </div>`;

export async function load() {
  await store.handlers.load();
  render();
  return store.handlers.data;
}

/** True once Plan has run and every shift with P2F flights has enough
 *  validly-nominated handlers. Before Plan has run there's nothing to
 *  gate on yet, so this returns true (the "no Plan yet" empty state is
 *  handled by /api/run itself, and by run.js disabling everything
 *  while a run is in flight). */
export function isReady() {
  const h = store.handlers.data;
  return !h || h.ready !== false;
}

/** Applies the current readiness to the Allocate button. Exported so
 *  run.js can re-derive it after clearing the "busy" disabled state,
 *  without needing a fresh fetch. */
export function applyAllocateGate() {
  const btn = $("#run-btn");
  if (!btn) return;
  const ready = isReady();
  btn.disabled = !ready;
  btn.title = ready
    ? ""
    : "Nominate a valid P2F handler for every shift listed below "
      + "(see the Handler panel) before allocating.";
}

function shiftCard(shift, h) {
  const status = (h.shift_status || {})[shift] || "not_needed";
  const required = (h.required_by_shift || {})[shift] || 0;
  const have = (h.valid_count_by_shift || {})[shift] || 0;
  const names = (h.p2f || [])
    .filter((p) => p.shift === shift)
    .map((p) => escapeHTML(p.name));

  if (status === "not_needed") {
    return `
      <div class="stat-card handler-card">
        <div class="stat-value">—</div>
        <div class="stat-label">P2F handler — <code>${escapeHTML(shift)}</code>
          <span class="subdued">(no P2F flights this shift)</span></div>
      </div>`;
  }
  if (status === "missing") {
    return `
      <div class="stat-card handler-card handler-missing">
        <div class="stat-value">${have} of ${required} nominated</div>
        <div class="stat-label">P2F handler — <code>${escapeHTML(shift)}</code>
          <span class="subdued">needs ${required - have} more</span></div>
      </div>`;
  }
  return `
    <div class="stat-card handler-card">
      <div class="stat-value">${names.join(", ") || `${have} of ${required}`}</div>
      <div class="stat-label">P2F handler — <code>${escapeHTML(shift)}</code>
        <span class="subdued">(${have} of ${required} needed)</span></div>
    </div>`;
}

export function render() {
  const h = store.handlers.data;
  const block = $("#handlers-block");
  if (block) {
    if (!h) {
      block.hidden = true;
    } else {
      const hasData = (h.p2f && h.p2f.length > 0)
        || Object.values(h.required_by_shift || {}).some((n) => n > 0);
      block.hidden = !hasData;
      if (hasData) {
        $("#handlers-date").textContent = h.run_date || "—";
        $("#handlers-grid").innerHTML =
          P2F_SHIFTS.map((s) => shiftCard(s, h)).join("");
        const issues = h.issues || [];
        $("#handlers-issues").innerHTML = issues.length
          ? `<ul class="handler-issues">${issues
              .map((msg) => `<li>${escapeHTML(msg)}</li>`)
              .join("")}</ul>`
          : "";
      }
    }
  }
  applyAllocateGate();
}
