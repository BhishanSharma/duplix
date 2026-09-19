/* Flight frequency through the operational day.
 *
 * 25 hourly buckets from 05:00 D through 05:00 D+1:
 *   bucket 0  = 05:00 D
 *   bucket 18 = 23:00 D
 *   bucket 19 = 00:00 D+1
 *   bucket 23 = 04:00 D+1
 *   bucket 24 = 05:00 D+1, where preplan-deferred 05:05-05:30 land
 *
 * A dual-handle range slider pins the visible window to any hour range.
 * Zooming re-draws from the cached buckets — it never re-fetches.
 */

import { $ } from "../core/dom.js";
import {
  SHIFT_COLOR,
  SHIFT_ORDER,
  SHIFT_WINDOWS_MIN,
  TREND_BUCKETS,
} from "../core/constants.js";

/** Plot geometry. The SVG scales to its container; these are viewBox
 *  units, not pixels. */
const W = 1200;
const H = 360;
const PAD_L = 50;
const PAD_R = 24;
const PAD_T = 36;
const PAD_B = 56;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;
const BAND_H = 14;

/** Keep at least this many buckets visible, so the slider can't
 *  collapse the plot to a single point. */
const MIN_VISIBLE = 2;

let zoom = { from: 0, to: TREND_BUCKETS };
let cached = null;      // last bucketed data, for zoom-only redraws

const bucketLabel = (b) => `${String((b + 5) % 24).padStart(2, "0")}:00`;
const dayMarker = (b) => (b + 5 >= 24 ? " D+1" : "");

function setZoom(from, to) {
  from = Math.max(0, Math.min(TREND_BUCKETS - 1, Math.floor(from)));
  to = Math.max(from + MIN_VISIBLE, Math.min(TREND_BUCKETS, Math.floor(to)));
  zoom = { from, to };
  if (cached) draw(cached);
}

function nextDayIso(dDay) {
  if (!dDay) return "";
  const [y, m, d] = dDay.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + 1));
  return [
    dt.getUTCFullYear(),
    String(dt.getUTCMonth() + 1).padStart(2, "0"),
    String(dt.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

/** Which bucket a row's STD falls in, or -1 if it's outside the window. */
function bucketFor(row, dDay, dPlus1) {
  const std = (row.std || "").trim();
  if (!std.includes(":")) return -1;
  const h = parseInt(std.split(":")[0], 10);
  if (isNaN(h) || h < 0 || h >= 24) return -1;
  const date = (row.date || "").trim();
  if (date === dDay && h >= 5) return h - 5;
  if (date === dPlus1 && h < 5) return h + 19;
  if (date === dPlus1 && h === 5) return 24;    // preplan-deferred tail
  return -1;
}

export function render(rows, dDay, wlRows) {
  const host = $("#ds-flight-trend");
  if (!host) return;
  if (!rows.length) {
    host.innerHTML =
      `<p class="subdued">No allocations yet — run Allocate to see the hourly trend.</p>`;
    cached = null;
    return;
  }

  const buckets = new Array(TREND_BUCKETS).fill(0);
  const dPlus1 = nextDayIso(dDay);
  for (const r of rows) {
    // Preplan-deferred rows (no staff but a planner) still represent
    // allocated workload. Truly unallocated rows — neither — don't.
    if (!r.staff && !r.planned_by) continue;
    const b = bucketFor(r, dDay, dPlus1);
    if (b >= 0 && b < TREND_BUCKETS) buckets[b]++;
  }

  const total = buckets.reduce((a, b) => a + b, 0);
  const max = Math.max(...buckets, 1);
  // Average over the operational day only (buckets 0-23). The 25th is
  // the preplan tail and would dilute the per-hour rate.
  const avg = buckets.slice(0, 24).reduce((a, b) => a + b, 0) / 24;

  cached = { buckets, total, max, avg, peakBucket: buckets.indexOf(max), wlRows };
  draw(cached);
}

function shiftBands(fromMin, toMin) {
  const minToX = (m) => {
    if (toMin === fromMin) return PAD_L;
    const clamped = Math.max(fromMin, Math.min(toMin, m));
    return PAD_L + (clamped - fromMin) / (toMin - fromMin) * PLOT_W;
  };
  const bandY = PAD_T - BAND_H - 4;
  return Object.entries(SHIFT_WINDOWS_MIN).map(([shift, [a, b]]) => {
    if (b < fromMin || a > toMin) return "";       // outside the window
    const x1 = minToX(a);
    const w = Math.max(0, minToX(b) - x1);
    if (w < 1) return "";
    const label = w > 18
      ? `<text x="${(x1 + w / 2).toFixed(1)}" y="${bandY + BAND_H - 3}"
               text-anchor="middle" class="trend-band-label">${shift}</text>`
      : "";
    return `
      <rect x="${x1.toFixed(1)}" y="${bandY}" width="${w.toFixed(1)}" height="${BAND_H}"
            fill="${SHIFT_COLOR[shift] || "#ccc"}" opacity="0.7" rx="2" />${label}`;
  }).join("");
}

/** The shift that owns a bucket's midpoint — the latest-starting one
 *  still active, so an overlap reads as the incoming shift. */
function dominantShift(absBucket) {
  const mid = 5 * 60 + absBucket * 60 + 30;
  const active = Object.entries(SHIFT_WINDOWS_MIN)
    .filter(([, [a, b]]) => a <= mid && mid <= b);
  if (!active.length) return null;
  active.sort((a, b) => b[1][0] - a[1][0]);
  return active[0][0];
}

function draw(data) {
  const host = $("#ds-flight-trend");
  if (!host) return;
  const { buckets, total, max, avg, peakBucket } = data;

  const from = Math.max(0, Math.min(TREND_BUCKETS - 1, zoom.from));
  const to = Math.max(from + 1, Math.min(TREND_BUCKETS, zoom.to));
  const visible = buckets.slice(from, to);
  const nVis = visible.length;
  const visMax = Math.max(...visible, 1);        // y-axis follows the visible peak
  const stepX = nVis > 1 ? PLOT_W / (nVis - 1) : PLOT_W;

  const bars = visible.map((n, i) => {
    const absI = from + i;
    const x = PAD_L + i * stepX - stepX * 0.4;
    const w = Math.max(2, stepX * 0.8);
    const h = n / visMax * PLOT_H;
    const color = SHIFT_COLOR[dominantShift(absI)] || "#7aa3d8";
    return `<rect x="${x.toFixed(1)}" y="${(PAD_T + PLOT_H - h).toFixed(1)}"
                  width="${w.toFixed(1)}" height="${h.toFixed(1)}"
                  fill="${color}" opacity="0.6" rx="2">
              <title>${bucketLabel(absI)} — ${n} flights</title>
            </rect>`;
  }).join("");

  const pts = visible.map((n, i) => [
    PAD_L + i * stepX,
    PAD_T + (PLOT_H - n / visMax * PLOT_H),
  ]);
  const linePath = pts.reduce((acc, [x, y], i) => {
    if (i === 0) return `M${x.toFixed(1)},${y.toFixed(1)}`;
    const [px, py] = pts[i - 1];
    const cx = (px + x) / 2;
    return `${acc} Q${cx.toFixed(1)},${py.toFixed(1)} ${x.toFixed(1)},${y.toFixed(1)}`;
  }, "");
  const areaPath = pts.length
    ? `${linePath} L${(PAD_L + (nVis - 1) * stepX).toFixed(1)},${PAD_T + PLOT_H} L${PAD_L},${PAD_T + PLOT_H} Z`
    : "";

  // The average line stays the FULL-DAY average — moving it with the
  // zoom window would make the reference meaningless.
  const avgY = PAD_T + (PLOT_H - (avg / visMax) * PLOT_H);
  const avgLine = avg <= visMax
    ? `<line x1="${PAD_L}" y1="${avgY.toFixed(1)}"
             x2="${PAD_L + PLOT_W}" y2="${avgY.toFixed(1)}"
             stroke="#c46500" stroke-width="1.2" stroke-dasharray="6 4" />
       <text x="${PAD_L + PLOT_W - 4}" y="${avgY - 4}"
             text-anchor="end" class="trend-avg-label">day avg ${avg.toFixed(1)}/hr</text>`
    : "";

  let peakCallout = "";
  if (peakBucket >= from && peakBucket < to) {
    const [px, py] = pts[peakBucket - from];
    peakCallout = `
      <circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="4"
              fill="#2563eb" stroke="#fff" stroke-width="2" />
      <text x="${px.toFixed(1)}" y="${(py - 8).toFixed(1)}"
            text-anchor="middle" class="trend-peak-label">peak ${max}</text>`;
  }

  // Ticks every ~3 hours, always including both edges so the operator
  // can read the window bounds.
  const xTicks = visible.map((_, i) => i)
    .filter((i) => i === 0 || i === nVis - 1 || (from + i) % 3 === 0)
    .map((i) => {
      const absI = from + i;
      const x = PAD_L + i * stepX;
      return `
        <line x1="${x}" y1="${PAD_T + PLOT_H}" x2="${x}" y2="${PAD_T + PLOT_H + 4}"
              stroke="#999" stroke-width="0.5" />
        <text x="${x}" y="${PAD_T + PLOT_H + 18}" text-anchor="middle" class="trend-axis">
          ${bucketLabel(absI)}${dayMarker(absI)}
        </text>`;
    }).join("");

  const yTicks = [
    [visMax, PAD_T + 4],
    [Math.round(visMax / 2), PAD_T + PLOT_H / 2 + 4],
    [0, PAD_T + PLOT_H + 4],
  ].map(([v, y]) =>
    `<text x="${PAD_L - 8}" y="${y}" text-anchor="end" class="trend-axis">${v}</text>`
  ).join("");

  const gridLines = [PAD_T, PAD_T + PLOT_H / 2].map((y) =>
    `<line x1="${PAD_L}" y1="${y}" x2="${PAD_L + PLOT_W}" y2="${y}"
           stroke="#eee" stroke-width="1" />`).join("");

  host.innerHTML = `
    ${zoomBar()}
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="trend-svg">
      <defs>
        <linearGradient id="trendGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#2563eb" stop-opacity="0.22" />
          <stop offset="100%" stop-color="#2563eb" stop-opacity="0" />
        </linearGradient>
      </defs>
      ${shiftBands(5 * 60 + from * 60, 5 * 60 + (to - 1) * 60)}
      ${gridLines}
      ${bars}
      <path d="${areaPath}" fill="url(#trendGrad)" />
      <path d="${linePath}" fill="none" stroke="#2563eb" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round" />
      <line x1="${PAD_L}" y1="${PAD_T + PLOT_H}" x2="${PAD_L + PLOT_W}" y2="${PAD_T + PLOT_H}"
            stroke="#aaa" stroke-width="0.8" />
      ${avgLine}
      ${peakCallout}
      ${xTicks}
      ${yTicks}
    </svg>
    <div class="trend-footer">
      <div class="trend-summary">
        <span class="trend-summary-cell"><strong>${total}</strong> total flights</span>
        <span class="trend-summary-cell"><strong>${avg.toFixed(1)}</strong> avg / hour</span>
        <span class="trend-summary-cell"><strong>${max}</strong> peak (${bucketLabel(peakBucket)})</span>
      </div>
      <div class="trend-shift-stats">${shiftStats(buckets)}</div>
    </div>`;

  wireZoom(host);
}

/** Per-shift totals in the footer — always full-day, never zoom-clipped,
 *  so the numbers don't change under the operator as they pan. */
function shiftStats(buckets) {
  return SHIFT_ORDER.filter((s) => SHIFT_WINDOWS_MIN[s]).map((s) => {
    const [a, b] = SHIFT_WINDOWS_MIN[s];
    const startB = Math.max(0, Math.floor((a - 5 * 60) / 60));
    const endB = Math.min(TREND_BUCKETS, Math.ceil((b - 5 * 60) / 60));
    let sum = 0;
    let hrs = 0;
    for (let i = startB; i < endB; i++) { sum += buckets[i]; hrs++; }
    const perHr = hrs > 0 ? (sum / hrs).toFixed(1) : "—";
    return `
      <div class="trend-stat-cell">
        <span class="shift-chip staff-shift-${s.toLowerCase()}">${s}</span>
        <span class="trend-stat-detail"><strong>${sum}</strong> flights · ${perHr}/hr</span>
      </div>`;
  }).join("");
}

/** Two stacked range inputs sharing one track: one sets the window
 *  start, one the end. */
function zoomBar() {
  const fromLabel = bucketLabel(zoom.from) + dayMarker(zoom.from);
  const toLabel = bucketLabel(zoom.to) + dayMarker(zoom.to);
  const isFull = zoom.from === 0 && zoom.to === TREND_BUCKETS;
  const reset = isFull
    ? '<span class="trend-zoom-reset disabled">Reset</span>'
    : '<button class="trend-zoom-reset" id="trend-zoom-reset">Reset</button>';
  const leftPct = (zoom.from / TREND_BUCKETS * 100).toFixed(1);
  const widthPct = ((zoom.to - zoom.from) / TREND_BUCKETS * 100).toFixed(1);
  return `
    <div class="trend-zoom-bar">
      <span class="trend-zoom-label">Zoom:</span>
      <span class="trend-zoom-window">
        <strong>${fromLabel}</strong> → <strong>${toLabel}</strong>
      </span>
      <div class="trend-zoom-slider">
        <div class="trend-zoom-track"></div>
        <div class="trend-zoom-fill" style="left: ${leftPct}%; width: ${widthPct}%;"></div>
        <input type="range" id="trend-zoom-from"
               min="0" max="${TREND_BUCKETS - MIN_VISIBLE}" step="1" value="${zoom.from}"
               aria-label="Zoom range start" />
        <input type="range" id="trend-zoom-to"
               min="${MIN_VISIBLE}" max="${TREND_BUCKETS}" step="1" value="${zoom.to}"
               aria-label="Zoom range end" />
      </div>
      ${reset}
    </div>`;
}

function wireZoom(host) {
  const fromSlider = host.querySelector("#trend-zoom-from");
  const toSlider = host.querySelector("#trend-zoom-to");
  if (fromSlider && toSlider) {
    fromSlider.addEventListener("input", () => {
      const f = Number(fromSlider.value);
      setZoom(f, Math.max(f + MIN_VISIBLE, Number(toSlider.value)));
    });
    toSlider.addEventListener("input", () => {
      const t = Number(toSlider.value);
      setZoom(Math.min(t - MIN_VISIBLE, Number(fromSlider.value)), t);
    });
  }
  host.querySelector("#trend-zoom-reset")
    ?.addEventListener("click", () => setZoom(0, TREND_BUCKETS));
}

/** Drop the cached buckets — called when the date or the run changes. */
export function clear() {
  cached = null;
}
