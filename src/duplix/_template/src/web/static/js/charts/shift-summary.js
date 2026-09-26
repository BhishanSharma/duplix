/* Shift summary table — per-shift flights, staff, average load, and the
 * category split.
 *
 * The split answers "how does this shift's volume break down", which a
 * single Flights number hides:
 *   P2F   TYPE=H flights
 *   I2D   DEP international, ARR domestic
 *   D2I   DEP domestic, ARR international
 *   T/F/C TEST + FERRY + CHARTER bundled
 *   Other regular domestic-to-domestic
 * An intl-to-intl flight counts as I2D, following the engine's
 * DEP-wins convention.
 */

import { $ } from "../core/dom.js";
import { SHIFT_ORDER } from "../core/constants.js";

const SPECIAL_CLASSES = new Set(["test", "ferry", "charter"]);

function emptyBucket() {
  return { staff: 0, flights: 0, actuals: [], p2f: 0, i2d: 0, d2i: 0, tfc: 0 };
}

function categorise(bucket, row) {
  const cls = (row.ops_class || "").toLowerCase();
  if (cls === "p2f") bucket.p2f++;
  else if (SPECIAL_CLASSES.has(cls)) bucket.tfc++;
  else if (row.is_international) bucket.i2d++;       // DEP wins
  else if (row.is_arr_international) bucket.d2i++;
}

export function render(wlRows, allocRows, shiftByStaff) {
  const host = $("#ds-shift-summary");
  if (!host) return;
  if (!wlRows.length && !allocRows.length) {
    host.innerHTML = `<p class="subdued">No data yet — run Allocate.</p>`;
    return;
  }

  const agg = Object.fromEntries(SHIFT_ORDER.map((s) => [s, emptyBucket()]));

  for (const r of wlRows) {
    const bucket = agg[r.shift];
    if (!bucket) continue;
    bucket.staff++;
    bucket.actuals.push(Number(r.actual || 0));
  }

  for (const r of allocRows) {
    const bucket = agg[shiftByStaff[(r.staff || "").trim()]];
    if (!bucket) continue;
    bucket.flights++;
    categorise(bucket, r);
  }

  const rows = SHIFT_ORDER
    .filter((s) => agg[s].staff > 0 || agg[s].flights > 0)
    .map((s) => {
      const a = agg[s];
      const avg = a.staff > 0 ? (a.flights / a.staff).toFixed(1) : "—";
      const range = a.actuals.length
        ? `${Math.min(...a.actuals)}-${Math.max(...a.actuals)}`
        : "—";
      const other = a.flights - a.p2f - a.i2d - a.d2i - a.tfc;
      return `<tr>
        <td><span class="shift-chip staff-shift-${s.toLowerCase()}">${s}</span></td>
        <td>${a.staff}</td>
        <td><strong>${a.flights}</strong></td>
        <td>${avg}</td>
        <td>${a.p2f || "—"}</td>
        <td>${a.i2d || "—"}</td>
        <td>${a.d2i || "—"}</td>
        <td>${a.tfc || "—"}</td>
        <td class="subdued">${other || "—"}</td>
        <td>${range}</td>
      </tr>`;
    }).join("");

  host.innerHTML = `
    <table class="shift-summary-table">
      <thead>
        <tr>
          <th>Shift</th><th>Staff</th><th>Flights</th><th>Avg/staff</th>
          <th title="P2F flights (TYPE=H)">P2F</th>
          <th title="DEP international, ARR domestic">I2D</th>
          <th title="DEP domestic, ARR international">D2I</th>
          <th title="TEST + FERRY + CHARTER bundled">T/F/C</th>
          <th title="Regular domestic-to-domestic">Other</th>
          <th>Range</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="subdued shift-summary-hint">
      <strong>Flights</strong> = P2F + I2D + D2I + T/F/C + Other.
      <strong>I2D</strong> = DEP international, ARR domestic.
      <strong>D2I</strong> = DEP domestic, ARR international.
      <strong>T/F/C</strong> = TEST + FERRY + CHARTER bundled.
      <strong>Range</strong> = min-to-max workload on that shift.
    </p>`;
}
