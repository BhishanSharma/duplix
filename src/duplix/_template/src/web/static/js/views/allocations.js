/* Allocations tab — one row per flight, with shift / INTL /
 * redistribution cues. */

import { $, escapeHTML } from "../core/dom.js";
import { SHIFT_ORDER } from "../core/constants.js";
import * as store from "../core/store.js";

export const id = "allocations";
export const label = "Allocations";

/** Rows rendered before the list is truncated. 5000 covers a full day
 *  on the heaviest schedules and still renders without DOM lag; the
 *  earlier 2000 was clipping D+1 NightOps rows past midnight. */
const MAX_RENDERED_ROWS = 5000;

/** NightOps means the post-midnight portion of the N shift only:
 *  00:00 - 05:30 on D+1 (N's allocatable end plus the 30 min handover
 *  tail). The pre-midnight evening portion is still an N-shift flight
 *  in the data, but the assigner reads that as the end of the day, so
 *  it only shows under "all". */
const NIGHT_OPS_END_MIN = 5 * 60 + 30;

export const template = `
  <div class="filters">
    <label>Sheet
      <select id="alloc-filter-sheet">
        <option value="">all</option>
        <option value="DayOps">DayOps</option>
        <option value="NightOps">NightOps</option>
        <option value="P2F">P2F</option>
        <option value="Removed">Removed (extracted via filter / Gulf)</option>
        <option value="__intl">International</option>
        <option value="__redistributed">Redistributed (changed vs prev iter)</option>
      </select>
    </label>
    <label>Search
      <input type="search" id="alloc-filter-q" placeholder="flight / staff / airport">
    </label>
    <label class="toggle">
      <input type="checkbox" id="alloc-only-warn"> warnings only
    </label>
    <span class="count" id="alloc-count">0 rows</span>
  </div>
  <div class="scroll-wrap">
    <table id="alloc-table">
      <thead>
        <tr>
          <th>Sheet</th><th>Flt</th><th>STD</th><th>Dep</th><th>Arr</th>
          <th>Pax</th><th>Staff</th><th>Pre-plan by</th><th>Relieved by</th>
          <th>Warning</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="shift-legend">
    <span class="shift-legend-label">Shift:</span>
    ${SHIFT_ORDER.map((s) =>
      `<span class="shift-chip staff-shift-${s.toLowerCase()}">${s}</span>`).join("\n    ")}
  </div>`;

export async function load() {
  await store.allocations.load();
  // Refresh the staff->shift index every time the tab opens. Cheap (one
  // read on the roster) and keeps the colours honest after a re-solve.
  await store.staffNames.load({ force: true });
  render();
}

/** Chronological: STD ascending, date as the tiebreaker so D+1
 *  night-tail flights at the same clock hour sort after D-day ones. */
function chronological(rows) {
  return rows.slice().sort((a, b) => {
    const da = a.date || "";
    const db = b.date || "";
    if (da !== db) return da < db ? -1 : 1;
    const sa = a.std || "";
    const sb = b.std || "";
    return sa < sb ? -1 : sa > sb ? 1 : 0;
  });
}

/** "__intl" and "__redistributed" are synthetic filters matched against
 *  row properties; every other value matches the sheet name. */
function matchesSheet(row, sheet) {
  if (!sheet) return true;
  if (sheet === "__intl") return !!row.is_international;
  if (sheet === "__redistributed") return !!row.redistributed_from;
  if (row.sheet !== sheet) return false;
  if (sheet !== "NightOps") return true;
  const [hh, mm] = (row.std || "").split(":");
  const stdMin = parseInt(hh, 10) * 60 + parseInt(mm || "0", 10);
  return Number.isFinite(stdMin) && stdMin <= NIGHT_OPS_END_MIN;
}

/** INTL = soft pink; ferry/test = light blue. */
function depArrClass(row) {
  if (row.is_international) return "cell-intl";
  const cls = String(row.ops_class || "").toLowerCase();
  return cls === "test" || cls === "ferry" ? "cell-special" : "";
}

function rowHtml(r, shiftByStaff) {
  // Colour every name by the NAMED person's shift — staff, pre-plan and
  // relieved-by cells alike — so the same person is the same colour
  // everywhere rather than taking the column's colour.
  const shiftClass = (nm) => {
    const sh = shiftByStaff[(nm || "").trim()] || "";
    return sh ? `staff-shift-${sh.toLowerCase()}` : "";
  };
  const cellClass = depArrClass(r);
  const from = r.redistributed_from || "";
  const staffTitle = from
    ? ` title="↻ Redistributed — was assigned to ${escapeHTML(from)} in previous iter"`
    : "";
  const badge = from
    ? `<span class="redist-badge" title="Redistributed from ${escapeHTML(from)}">↻</span>`
    : "";
  return `<tr${from ? ' class="row-redistributed"' : ""}>
    <td>${escapeHTML(r.sheet)}</td>
    <td>${escapeHTML(r.flt)}</td>
    <td>${escapeHTML(r.std)}</td>
    <td class="${cellClass}">${escapeHTML(r.dep)}</td>
    <td class="${cellClass}">${escapeHTML(r.arr)}</td>
    <td>${r.pax || ""}</td>
    <td class="${shiftClass(r.staff)}"${staffTitle}>${escapeHTML(r.staff)}${badge}</td>
    <td class="${shiftClass(r.planned_by)}">${escapeHTML(r.planned_by)}</td>
    <td class="${shiftClass(r.relieved_by)}">${escapeHTML(r.relieved_by)}</td>
    <td class="${r.warning ? "cell-warn" : ""}">${escapeHTML(r.warning)}</td>
  </tr>`;
}

export function render() {
  const sheet = $("#alloc-filter-sheet").value;
  const q = $("#alloc-filter-q").value.trim().toUpperCase();
  const onlyWarn = $("#alloc-only-warn").checked;
  const shiftByStaff = store.shiftByStaff();

  const lines = [];
  let count = 0;
  for (const r of chronological(store.allocations.data)) {
    if (!matchesSheet(r, sheet)) continue;
    if (onlyWarn && !r.warning) continue;
    if (q) {
      const blob =
        `${r.flt} ${r.staff} ${r.dep} ${r.arr} ${r.planned_by} ${r.relieved_by}`
          .toUpperCase();
      if (!blob.includes(q)) continue;
    }
    count++;
    if (count > MAX_RENDERED_ROWS) continue;
    lines.push(rowHtml(r, shiftByStaff));
  }

  $("#alloc-table tbody").innerHTML = lines.join("");
  const total = store.allocations.data.length;
  $("#alloc-count").textContent = count > MAX_RENDERED_ROWS
    ? `${count.toLocaleString()} matching rows (showing first ${MAX_RENDERED_ROWS} of ${total.toLocaleString()})`
    : `${count.toLocaleString()} matching rows of ${total.toLocaleString()}`;
}

export function init() {
  $("#alloc-filter-sheet").addEventListener("change", render);
  $("#alloc-filter-q").addEventListener("input", render);
  $("#alloc-only-warn").addEventListener("change", render);
}
