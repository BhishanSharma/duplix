/* Roster by shift — per-shift name cards for visual reference.
 *
 * ZCs render first with a green chip, STAFF below. ZCs come from the AM
 * roster so their role reads 'AM' with the ZC marker in the status;
 * the API's is_zc flag is the reliable signal, with the status text as
 * a fallback. Pure-AM staff don't fly, so they're left out.
 */

import { $, escapeHTML } from "../core/dom.js";
import { SHIFT_ORDER } from "../core/constants.js";

function isZC(row) {
  const status = (row.status || "").trim().toUpperCase();
  const role = (row.role || "").trim().toUpperCase();
  return row.is_zc === true || status.includes("ZC") || role === "ZC";
}

export function render(names) {
  const host = $("#ds-roster");
  if (!host) return;
  if (!names.length) {
    host.innerHTML = `<p class="subdued">No roster yet — run Plan or Allocate.</p>`;
    return;
  }

  const buckets = {};        // shift -> {zc: [], staff: []}
  for (const r of names) {
    const shift = (r.shift || "").trim();
    if (!shift) continue;
    const zc = isZC(r);
    if ((r.role || "").trim().toUpperCase() === "AM" && !zc) continue;
    const name = (r.name || "").trim();
    if (!name) continue;
    const bucket = (buckets[shift] = buckets[shift] || { zc: [], staff: [] });
    (zc ? bucket.zc : bucket.staff).push(name);
  }

  for (const bucket of Object.values(buckets)) {
    bucket.zc.sort();
    bucket.staff.sort();
  }

  const shifts = SHIFT_ORDER.filter((s) => buckets[s]);
  if (!shifts.length) {
    host.innerHTML = `<p class="subdued">No assignable staff on the roster.</p>`;
    return;
  }

  host.innerHTML = shifts.map((shift) => {
    const b = buckets[shift];
    const zcChips = b.zc.map((n) =>
      `<span class="roster-name roster-name-zc" title="ZC on shift ${shift}">${escapeHTML(n)} <span class="roster-zc-tag">ZC</span></span>`
    ).join("");
    const staffChips = b.staff.map((n) =>
      `<span class="roster-name">${escapeHTML(n)}</span>`
    ).join("");
    const empty = b.zc.length ? "" : '<span class="subdued">(no staff)</span>';
    const total = b.zc.length + b.staff.length;
    return `
      <div class="roster-shift-card staff-shift-${shift.toLowerCase()}">
        <div class="roster-shift-header">
          <span class="roster-shift-label">${shift}</span>
          <span class="roster-shift-count">${total} total · ${b.zc.length} ZC · ${b.staff.length} STAFF</span>
        </div>
        <div class="roster-shift-body">
          ${zcChips}${staffChips || empty}
        </div>
      </div>`;
  }).join("");
}
