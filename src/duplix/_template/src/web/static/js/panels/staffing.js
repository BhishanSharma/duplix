/* Staffing recommendation — people needed per shift pool.
 *
 * Judged on the busiest 15-minute slots, not hourly averages: one person
 * can take one flight per 15 minutes, so a slot with more departures
 * than people who can take them forces squeezed flights. REMOVE only
 * shows when every slot stays comfortable without those people (see
 * pipeline/recommender_staffing.py).
 */

import { $, escapeHTML } from "../core/dom.js";
import * as store from "../core/store.js";

export const template = `
  <div class="block" id="staffing-block" hidden>
    <h2>Staffing recommendation — <span id="staffing-date">—</span></h2>
    <p class="subdued">Judged on the <strong>busiest 15 minutes</strong>, not
      hourly averages: one person can take one flight per 15 min, so when
      most of the people who can take a slot's flights are needed at once,
      there is nobody to spare. Above ~85% the engine starts squeezing
      flights (spacing relaxed).</p>
    <div class="stat-grid staffing-grid" id="staffing-grid"></div>
  </div>`;

export async function load() {
  await store.staffing.load();
  render();
  return store.staffing.data;
}

/** Red: add people. Blue: can remove. Amber: OK but no one to spare.
 *  Green: OK with room. */
function actionClass(row) {
  if (row.action.startsWith("ADD")) return "staffing-add";
  if (row.action.startsWith("REMOVE")) return "staffing-remove";
  return row.peak_use_pct > 85 ? "staffing-tight" : "staffing-ok";
}

function headline(row) {
  if (row.action.startsWith("ADD") || row.action.startsWith("REMOVE")) {
    return row.breakdown_action;
  }
  return row.peak_use_pct > 85 ? "OK — no one to spare" : "OK";
}

function meterClass(pct) {
  if (pct > 100) return "over";
  if (pct > 85) return "tight";
  return "fine";
}

function card(row) {
  const pct = Number(row.peak_use_pct) || 0;
  const p2fBadge = row.has_p2f_flight
    ? '<span class="badge subdued staffing-p2f-badge">P2F flight present</span>'
    : "";
  const tight = row.tight_windows
    ? `<div class="staffing-line"><span class="subdued">Also tight:</span> ${escapeHTML(row.tight_windows)}</div>`
    : "";
  const squeezed = row.squeezed
    ? `<div class="staffing-line staffing-squeezed">Last Allocate squeezed
         ${row.squeezed} flight(s) here: ${escapeHTML(row.squeezed_times)}</div>`
    : "";
  const comfy = row.comfortable > row.assigned_today
    ? ` · for no squeezed flights ≈ ${row.comfortable}`
    : "";
  return `
    <div class="stat-card staffing-card ${actionClass(row)}">
      <div class="stat-label">
        ${escapeHTML(row.pool)} (${escapeHTML(row.shifts_in_pool)}) ${p2fBadge}
      </div>
      <div class="staffing-headline">${escapeHTML(headline(row))}</div>
      ${row.peak_window ? `
      <div class="staffing-peak">
        <div class="staffing-peak-text">
          Busiest 15 min <strong>${escapeHTML(row.peak_window)}</strong>:
          ${row.peak_window_flights} flights · ${row.peak_window_people} people
          <strong>${pct}%</strong>
        </div>
        <div class="staffing-meter ${meterClass(pct)}" role="img"
             aria-label="${pct}% of the people who can take these flights">
          <span style="width:${Math.min(pct, 100)}%"></span>
          <i style="left:85%" title="~85%: squeezing starts above this"></i>
        </div>
      </div>` : ""}
      <p class="staffing-why">${escapeHTML(row.explanation)}</p>
      ${tight}
      ${squeezed}
      <div class="staffing-detail">
        Rostered ${row.assigned_today} (${escapeHTML(row.assigned_breakdown)})
        · total volume alone needs ${row.volume_floor}${comfy}
      </div>
    </div>`;
}

export function render() {
  const s = store.staffing.data;
  const block = $("#staffing-block");
  if (!block) return;
  if (!s || !s.rows || s.rows.length === 0) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const dateEl = $("#staffing-date");
  if (dateEl) dateEl.textContent = s.run_date || "—";
  $("#staffing-grid").innerHTML = s.rows.map(card).join("");
}
