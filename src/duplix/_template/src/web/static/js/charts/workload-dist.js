/* Workload distribution — vertical stacked bars, one per shift.
 *
 * STAFF and ZC merged: the question the assigner is asking is "how is
 * today's load spread across the people on this shift", and splitting
 * by role made two short bars instead of one readable one.
 */

import { $ } from "../core/dom.js";
import {
  FALLBACK_LEVEL_PALETTE,
  SHIFT_LEVEL_PALETTE,
  SHIFT_ORDER,
} from "../core/constants.js";

/** A segment narrower than this gets no inline label — the text would
 *  overflow its own band. */
const MIN_LABEL_PCT = 12;

function colorForLevel(shift, i, n) {
  const palette = SHIFT_LEVEL_PALETTE[shift] || FALLBACK_LEVEL_PALETTE;
  // i = 0 is the highest level (top of the bar); map highest → darkest.
  const idx = Math.min(
    palette.length - 1,
    Math.round((n - 1 - i) / Math.max(n - 1, 1) * (palette.length - 1)),
  );
  return palette[idx];
}

export function render(rows) {
  const host = $("#ds-workload-dist");
  if (!host) return;

  // shift -> {workload level: staff count}
  const buckets = {};
  for (const r of rows) {
    if (!r.shift) continue;
    const dist = (buckets[r.shift] = buckets[r.shift] || {});
    const level = Number(r.actual || 0);
    dist[level] = (dist[level] || 0) + 1;
  }

  const shifts = SHIFT_ORDER.filter((s) => buckets[s]);
  if (!shifts.length) {
    host.innerHTML = `<p class="subdued">No workload data yet — run Allocate.</p>`;
    return;
  }

  // Scale every bar to the largest shift's headcount so the columns
  // stay visually comparable.
  const totals = shifts.map((s) =>
    Object.values(buckets[s]).reduce((a, b) => a + b, 0));
  const maxTotal = Math.max(...totals);

  const bars = shifts.map((shift, idx) => {
    const dist = buckets[shift];
    const total = totals[idx];
    // Descending so the heaviest level stacks at the top of the bar.
    const levels = Object.keys(dist).map(Number).sort((a, b) => b - a);
    const segs = levels.map((level, i) => {
      const pct = dist[level] / total * 100;
      const color = colorForLevel(shift, i, levels.length);
      const label = pct >= MIN_LABEL_PCT
        ? `<span class="vbar-seg-label">${level}</span>`
        : "";
      return `<div class="vbar-seg" style="height:${pct.toFixed(1)}%; background:${color};"
                   title="level ${level}: ${dist[level]} staff (${pct.toFixed(0)}%)">
                ${label}
              </div>`;
    }).join("");
    const heightPct = (total / maxTotal * 100).toFixed(0);
    return `
      <div class="vbar-col">
        <div class="vbar-stack">
          <div class="vbar" style="height:${heightPct}%;">${segs}</div>
        </div>
        <div class="vbar-label">${shift}</div>
        <div class="vbar-count">${total} present</div>
      </div>`;
  }).join("");

  host.innerHTML = `<div class="vbar-row">${bars}</div>`;
}
