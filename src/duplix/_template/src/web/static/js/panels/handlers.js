/* Nominated handlers — the P2F slot per shift, plus the single NORSE
 * slot.
 *
 * P2F handlers are auto-picked from STAFF roster cells marked M/P2F,
 * A/P2F, N/P2F. NORSE has no roster shorthand: it comes from an
 * override row, or the engine auto-picks a non-N ZC.
 */

import { $, escapeHTML } from "../core/dom.js";
import * as store from "../core/store.js";

/** Shifts that get a P2F handler card, in display order. */
const P2F_SHIFTS = ["M", "A", "N"];

export const template = `
  <div class="block" id="handlers-block" hidden>
    <h2>Nominated handlers — <span id="handlers-date">—</span></h2>
    <p class="subdued">P2F handlers are auto-picked from STAFF roster cells
      marked <code>M/P2F</code>, <code>A/P2F</code>, <code>N/P2F</code>.
      NORSE handler comes from an override row.
      Edit via the <strong>Override</strong> drawer (top right).</p>
    <div class="stat-grid" id="handlers-grid"></div>
  </div>`;

export async function load() {
  await store.handlers.load();
  // Any handler data at all proves Step 1+2 has run, so unhide the
  // Override button from here too — belt and braces, in case the plan
  // readback failed and left the UI without a way in.
  const h = store.handlers.data;
  if (h) {
    const planRan = (h.p2f && h.p2f.length > 0)
      || (h.norse && h.norse.length > 0)
      || (h.run_date && h.run_date !== "");
    if (planRan) $("#open-override").hidden = false;
  }
  render();
  return h;
}

function p2fCard(shift, nominee) {
  if (!nominee) {
    return `
      <div class="stat-card handler-card handler-missing">
        <div class="stat-value">— not nominated —</div>
        <div class="stat-label">P2F handler — <code>${escapeHTML(shift)}</code></div>
      </div>`;
  }
  const src = nominee.source === "roster" ? "from roster" : "from Override";
  return `
    <div class="stat-card handler-card">
      <div class="stat-value">${escapeHTML(nominee.name)}</div>
      <div class="stat-label">P2F handler — <code>${escapeHTML(shift)}</code>
        <span class="subdued">(${escapeHTML(src)})</span></div>
    </div>`;
}

function norseCards(norse) {
  if (!norse.length) {
    return `
      <div class="stat-card handler-card handler-missing">
        <div class="stat-value">— not assigned —</div>
        <div class="stat-label">NORSE handler</div>
      </div>`;
  }
  return norse.map((n) => {
    const isAuto = n.source === "auto-pick";
    const src = isAuto
      ? '<span class="subdued">(auto-picked — lock via Override if needed)</span>'
      : '<span class="subdued">(from Override)</span>';
    return `
      <div class="stat-card handler-card${isAuto ? " handler-auto" : ""}">
        <div class="stat-value">${escapeHTML(n.name)}</div>
        <div class="stat-label">NORSE handler ${src}</div>
      </div>`;
  }).join("");
}

export function render() {
  const h = store.handlers.data;
  const block = $("#handlers-block");
  if (!block) return;
  if (!h) { block.hidden = true; return; }

  // Show whenever there's data. This used to gate on the Override
  // button being visible ("plan has run"), which race-conditioned with
  // the plan readback: handlers could load first, see the button still
  // hidden, and hide the block despite having data to show.
  const hasData = (h.p2f && h.p2f.length > 0)
    || (h.norse && h.norse.length > 0)
    || (h.missing_p2f_shifts && h.missing_p2f_shifts.length > 0);
  if (!hasData) { block.hidden = true; return; }

  block.hidden = false;
  $("#handlers-date").textContent = h.run_date || "—";
  const byShift = new Map((h.p2f || []).map((p) => [p.shift, p]));
  $("#handlers-grid").innerHTML =
    P2F_SHIFTS.map((s) => p2fCard(s, byShift.get(s))).join("")
    + norseCards(h.norse || []);
}
