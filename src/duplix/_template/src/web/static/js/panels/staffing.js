/* Staffing recommendation — required headcount per pool.
 *
 * Driven by peak hour spacing (H10), total volume against the hard cap,
 * and P2F handler presence. No sick/surge buffer: operational slack is
 * handled separately. An action only shows when the gap is outside ±2.
 */

import { $, escapeHTML } from "../core/dom.js";
import * as store from "../core/store.js";

export const template = `
  <div class="block" id="staffing-block" hidden>
    <h2>Staffing recommendation — <span id="staffing-date">—</span></h2>
    <p class="subdued">Per-pool required headcount based on
      <strong>peak hour spacing (H10)</strong>, <strong>total volume vs hard cap</strong>,
      and <strong>P2F handler presence</strong>. No sick/surge buffer — operational
      slack is handled separately. Action shown if gap is outside ±2.</p>
    <div class="stat-grid" id="staffing-grid"></div>
  </div>`;

export async function load() {
  await store.staffing.load();
  render();
  return store.staffing.data;
}

/** ADD reads as a warning, REMOVE as information, OK as neutral. */
function actionClass(action) {
  if (action.startsWith("ADD")) return "handler-missing";
  if (action.startsWith("REMOVE")) return "handler-auto";
  return "";
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

  $("#staffing-grid").innerHTML = s.rows.map((row) => {
    const p2fBadge = row.has_p2f_flight
      ? '<span class="badge subdued staffing-p2f-badge">P2F flight present</span>'
      : "";
    return `
      <div class="stat-card handler-card ${actionClass(row.action)}">
        <div class="stat-value">${escapeHTML(String(row.required))}</div>
        <div class="stat-label">
          ${escapeHTML(row.pool)} (${escapeHTML(row.shifts_in_pool)}) ${p2fBadge}
        </div>
        <div class="stat-sub staffing-detail">
          <strong>${escapeHTML(row.action)}</strong> — <em>${escapeHTML(row.breakdown_action)}</em><br>
          currently ${row.assigned_today} (${escapeHTML(row.assigned_breakdown)}) →
          target ${row.required} (${escapeHTML(row.target_breakdown)})<br>
          ${row.flights_in_window} flights · peak ${escapeHTML(row.peak_hour)} = ${row.peak_hour_flights}<br>
          floors: peak ${row.peak_floor} · vol ${row.volume_floor} · p2f ${row.p2f_floor}
          (driver: <em>${escapeHTML(row.bottleneck)}</em>)<br>
          target/staff ${row.target_per_staff} · cap ${row.hard_cap_per_staff} ·
          implied avg if accepted: ${row.implied_avg_per_staff}
        </div>
      </div>`;
  }).join("");
}
