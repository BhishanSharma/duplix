/* Flights distributed per shift — share of the day's allocated flights
 * each shift picked up.
 */

import { $ } from "../core/dom.js";
import { SHIFT_COLOR, SHIFT_ORDER } from "../core/constants.js";

const CX = 100;
const CY = 100;
const R = 80;

/** Slices thinner than this get no in-slice label; the legend carries
 *  the number instead. */
const MIN_LABEL_PCT = 8;

export function render(allocRows, shiftByStaff) {
  const host = $("#ds-flights-pie");
  if (!host) return;

  const counts = Object.fromEntries(SHIFT_ORDER.map((s) => [s, 0]));
  for (const r of allocRows) {
    const shift = shiftByStaff[(r.staff || "").trim()];
    if (shift && counts[shift] !== undefined) counts[shift]++;
  }

  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (total === 0) {
    host.innerHTML = `<p class="subdued">No allocations yet — run Allocate.</p>`;
    return;
  }

  const present = SHIFT_ORDER.filter((s) => counts[s] > 0);
  let cum = -Math.PI / 2;    // start at 12 o'clock

  const slices = present.map((shift) => {
    const n = counts[shift];
    const angle = n / total * Math.PI * 2;
    const x1 = CX + R * Math.cos(cum);
    const y1 = CY + R * Math.sin(cum);
    cum += angle;
    const x2 = CX + R * Math.cos(cum);
    const y2 = CY + R * Math.sin(cum);
    const large = angle > Math.PI ? 1 : 0;
    const pct = (n / total * 100).toFixed(0);
    const path = `M${CX},${CY} L${x1.toFixed(1)},${y1.toFixed(1)}
                  A${R},${R} 0 ${large} 1 ${x2.toFixed(1)},${y2.toFixed(1)} Z`;
    // Label sits at the arc midpoint, halfway out along the radius.
    // Shift name only — the percentage lives in the legend.
    const mid = cum - angle / 2;
    const lx = CX + R * 0.6 * Math.cos(mid);
    const ly = CY + R * 0.6 * Math.sin(mid);
    const label = pct >= MIN_LABEL_PCT
      ? `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}"
               text-anchor="middle" dominant-baseline="central"
               class="pie-slice-label">${shift}</text>`
      : "";
    return `<path d="${path}" fill="${SHIFT_COLOR[shift]}" stroke="#fff" stroke-width="2">
              <title>${shift}: ${n} flights (${pct}%)</title>
            </path>${label}`;
  }).join("");

  const legend = present.map((shift) => `
    <div class="pie-legend-item">
      <span class="pie-swatch" style="background:${SHIFT_COLOR[shift]}"></span>
      <span class="pie-shift">${shift}</span>
      <span class="pie-count">${counts[shift]} (${(counts[shift] / total * 100).toFixed(0)}%)</span>
    </div>`).join("");

  host.innerHTML = `
    <svg viewBox="0 0 200 200" class="pie-svg">${slices}</svg>
    <div class="pie-legend">${legend}</div>
    <div class="pie-total subdued">Total: ${total} flights</div>`;
}
