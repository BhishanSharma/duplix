/* Workload tab — per-staff load against the preferred / cap bands. */

import { $, escapeHTML } from "../core/dom.js";
import { SHIFT_OPTIONS } from "../core/constants.js";
import * as store from "../core/store.js";

export const id = "workload";
export const label = "Workload";

export const template = `
  <div class="filters">
    <label>Sort
      <select id="wl-sort">
        <option value="dev_above">over preferred</option>
        <option value="dev_below">under preferred</option>
        <option value="name">name</option>
      </select>
    </label>
    <label>Shift
      <select id="wl-filter-shift">
        <option value="">all</option>
        ${SHIFT_OPTIONS.map((s) => `<option>${s}</option>`).join("")}
      </select>
    </label>
    <label>Name
      <input type="search" id="wl-filter-name" placeholder="partial, case-insensitive">
    </label>
    <span class="count" id="wl-count">0 staff</span>
  </div>
  <div class="scroll-wrap">
    <table id="wl-table">
      <thead>
        <tr>
          <th>Name</th><th>Shift</th><th>Role</th>
          <th>Target</th><th>Acceptable</th><th>Cap</th>
          <th>Actual</th><th>Δ</th><th>Bar</th><th>Violations</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>`;

// "under preferred" / "over preferred" are FILTERS, not just sort
// orders: sorting the full list left +0 / +1 / +2 rows at the bottom of
// an "under preferred" view, which read as a contradiction.
const FILTERS = {
  dev_below: (r) => r.actual < r.target_preferred,
  dev_above: (r) => r.actual > r.target_preferred,
};

const COMPARATORS = {
  // Most-over-preferred first when filtering to over.
  dev_above: (a, b) =>
    (b.actual - b.target_preferred) - (a.actual - a.target_preferred),
  // Most-under-preferred first when filtering to under.
  dev_below: (a, b) =>
    (a.actual - a.target_preferred) - (b.actual - b.target_preferred),
  name: (a, b) => a.name.localeCompare(b.name),
};

export async function load() {
  await store.workload.load();
  render();
}

function barCell(r, max) {
  const fillPct = Math.min(100, (r.actual / max) * 100);
  const markPref = (r.target_preferred / max) * 100;
  const markCap = (r.hard_cap / max) * 100;
  let fillClass = "fill";
  if (r.actual > r.hard_cap) fillClass += " over-cap";
  else if (r.actual > r.target_acceptable_max) fillClass += " over";
  return `
    <div class="bar" title="actual=${r.actual}, target=${r.target_preferred}, cap=${r.hard_cap}">
      <div class="${fillClass}" style="width:${fillPct}%;"></div>
      <span class="marker" style="left:${markPref}%;"></span>
      <span class="marker" style="left:${markCap}%;background:rgba(207,34,46,0.5);"></span>
    </div>`;
}

export function render() {
  const sortKey = $("#wl-sort").value;
  const shift = $("#wl-filter-shift").value;
  const nameQ = ($("#wl-filter-name")?.value || "").trim().toLowerCase();

  let rows = store.workload.data.slice();
  if (shift) rows = rows.filter((r) => r.shift === shift);
  if (nameQ) rows = rows.filter((r) => (r.name || "").toLowerCase().includes(nameQ));
  const filter = FILTERS[sortKey];
  if (filter) rows = rows.filter(filter);
  rows.sort(COMPARATORS[sortKey] || (() => 0));

  const max = Math.max(1, ...rows.map((r) => Math.max(r.actual, r.hard_cap)));
  $("#wl-table tbody").innerHTML = rows.map((r) => {
    const dev = r.actual - r.target_preferred;
    return `
      <tr>
        <td>${escapeHTML(r.name)}</td>
        <td>${escapeHTML(r.shift)}</td>
        <td>${escapeHTML(r.role)}</td>
        <td>${r.target_preferred}</td>
        <td>${r.target_acceptable_max}</td>
        <td>${r.hard_cap}</td>
        <td>${r.actual}</td>
        <td>${dev >= 0 ? "+" : ""}${dev}</td>
        <td>${barCell(r, max)}</td>
        <td>${escapeHTML(r.violations)}</td>
      </tr>`;
  }).join("");
  $("#wl-count").textContent = `${rows.length} staff`;
}

export function init() {
  $("#wl-sort").addEventListener("change", render);
  $("#wl-filter-shift").addEventListener("change", render);
  $("#wl-filter-name")?.addEventListener("input", render);
}
