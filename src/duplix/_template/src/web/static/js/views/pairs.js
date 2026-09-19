/* Pairs tab — the pair map at every shift boundary. */

import { $, escapeHTML } from "../core/dom.js";
import { SHIFT_OPTIONS } from "../core/constants.js";
import * as store from "../core/store.js";

export const id = "pairs";
export const label = "Pairs";

const shiftOptions = SHIFT_OPTIONS.map((s) => `<option>${s}</option>`).join("");

export const template = `
  <p class="subdued">
    Pairs at every shift boundary. Grey rows are handover-only (no
    pre-planning). Split-1+2 means one boundary has two pairs sharing a
    partner; that partner is duplicated across both rows on purpose —
    round-3 design. ZC cross-role only appears when |M-ZC| &gt; |natural N|
    and night staff need to cover excess M-ZC pre-planning.
  </p>
  <div class="filters">
    <label>Search name
      <input type="search" id="pairs-filter-q" placeholder="partial, case-insensitive">
    </label>
    <label>From shift
      <select id="pairs-filter-from">
        <option value="">All</option>${shiftOptions}
      </select>
    </label>
    <label>To shift
      <select id="pairs-filter-to">
        <option value="">All</option>${shiftOptions}
      </select>
    </label>
    <button id="pairs-clear" class="ghost" type="button">Clear filters</button>
    <span class="count" id="pairs-count">0 pairs</span>
  </div>
  <div class="scroll-wrap">
    <table id="pairs-table">
      <thead>
        <tr>
          <th>Boundary</th><th>Role</th><th>Pre-plan #</th>
          <th>Prev (employee)</th><th>Prev shift</th>
          <th>Next (employee)</th><th>Next shift</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>`;

export async function load() {
  await store.pairs.load();
  render();
}

export function render() {
  const q = $("#pairs-filter-q").value.trim().toUpperCase();
  const fromShift = $("#pairs-filter-from").value;
  const toShift = $("#pairs-filter-to").value;
  const rows = store.pairs.data;
  const tbody = $("#pairs-table tbody");
  $("#pairs-table").classList.add("row-handover");

  const matched = [];
  for (const r of rows) {
    if (fromShift && r.prev_shift !== fromShift) continue;
    if (toShift && r.next_shift !== toShift) continue;
    if (q && !`${r.prev_name} ${r.next_name}`.toUpperCase().includes(q)) continue;
    const handover = r.preplan_count === 0;
    matched.push(`
      <tr class="${handover ? "handover" : ""}">
        <td>${escapeHTML(r.boundary)}</td>
        <td>${escapeHTML(r.pair_role)}</td>
        <td>${r.preplan_count}</td>
        <td>${escapeHTML(r.prev_name)} <span class="subdued">(${escapeHTML(r.prev_employee_id)})</span></td>
        <td>${escapeHTML(r.prev_shift)}</td>
        <td>${escapeHTML(r.next_name)} <span class="subdued">(${escapeHTML(r.next_employee_id)})</span></td>
        <td>${escapeHTML(r.next_shift)}</td>
      </tr>`);
  }

  const count = matched.length;
  $("#pairs-count").textContent = `${count} pair${count === 1 ? "" : "s"}`;
  if (count) {
    tbody.innerHTML = matched.join("");
    return;
  }
  // Distinguish "filters returned nothing" from "no pairs at all".
  const hasFilter = q || fromShift || toShift;
  const msg = rows.length === 0
    ? "No pairs — run the pipeline first."
    : hasFilter
      ? "No pairs match the current filters. Try Clear filters."
      : "No pairs.";
  tbody.innerHTML = `<tr><td colspan="7" class="subdued">${msg}</td></tr>`;
}

function clearFilters() {
  $("#pairs-filter-q").value = "";
  $("#pairs-filter-from").value = "";
  $("#pairs-filter-to").value = "";
  render();
}

export function init() {
  $("#pairs-filter-q").addEventListener("input", render);
  $("#pairs-filter-from").addEventListener("change", render);
  $("#pairs-filter-to").addEventListener("change", render);
  $("#pairs-clear").addEventListener("click", clearFilters);
}
