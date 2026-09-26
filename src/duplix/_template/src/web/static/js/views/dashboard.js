/* Dashboard tab — the aggregate view.
 *
 * Reading order down the page: inputs, headline counts, per-class
 * counts, charts, nominated handlers, staffing recommendation, workload
 * review, run status. The panels/ modules own the blocks that have
 * their own load cycle; this view owns the layout and the stat cards.
 */

import { $, escapeHTML, setText } from "../core/dom.js";
import * as store from "../core/store.js";
import * as inputsPanel from "../panels/inputs.js";
import * as handlersPanel from "../panels/handlers.js";
import * as staffingPanel from "../panels/staffing.js";
import * as planPanel from "../panels/plan.js";
import * as workloadDist from "../charts/workload-dist.js";
import * as flightsPie from "../charts/flights-pie.js";
import * as shiftSummary from "../charts/shift-summary.js";
import * as flightTrend from "../charts/flight-trend.js";
import * as rosterChart from "../charts/roster.js";
import * as zcSelector from "../panels/zc-selector.js";
import * as rosterEdit from "../panels/roster-edit.js";
import * as handlerAssign from "../panels/handler-assign.js";
import * as unallocatedView from "./unallocated.js";

export const id = "dashboard";
export const label = "Dashboard";

/** Headline cards: element id -> the stats key it reads. */
const HEADLINE_STATS = {
  "ds-flights-total": "flights_total",
  "ds-flights-unallocated": "flights_unallocated",
  "ds-staff-total": "staff_total",
  "ds-staff-at-risk": "staff_at_risk",
  "ds-warnings-total": "warnings_total",
  "ds-day-ops": "day_ops",
  "ds-night-ops": "night_ops",
  "ds-p2f": "p2f",
  "ds-special-ops": "special_ops",
  "ds-special-ops-test": "special_ops_test",
  "ds-special-ops-ferry": "special_ops_ferry",
  "ds-special-ops-charter": "special_ops_charter",
};

/** Solver statuses that are a good outcome — anything else is flagged. */
const GOOD_SOLVER_STATES = ["OPTIMAL", "FEASIBLE"];

const statCard = (id, label, cls = "") => `
  <div class="stat-card">
    <div class="stat-value${cls ? ` ${cls}` : ""}" id="${id}">—</div>
    <div class="stat-label">${label}</div>
  </div>`;

export const template = `
  <div class="stat-grid">
    ${statCard("ds-flights-total", "Flights allocated")}
    ${statCard("ds-flights-unallocated", "Flights unallocated", "warn")}
    ${statCard("ds-staff-total", "Active staff")}
    ${statCard("ds-staff-at-risk", "Workload review", "warn")}
    ${statCard("ds-warnings-total", "Warnings")}
  </div>

  <div class="stat-grid stat-grid-secondary">
    ${statCard("ds-day-ops", "Day Ops to plan")}
    ${statCard("ds-night-ops", "Night Ops to plan")}
    ${statCard("ds-p2f", "P2F to plan")}
    <div class="stat-card">
      <div class="stat-value" id="ds-special-ops">—</div>
      <div class="stat-label">Special Ops to plan</div>
      <div class="stat-sub stat-sub-inline">
        T: <span id="ds-special-ops-test">—</span>
        &nbsp;·&nbsp;
        F: <span id="ds-special-ops-ferry">—</span>
        &nbsp;·&nbsp;
        C: <span id="ds-special-ops-charter">—</span>
      </div>
    </div>
  </div>

  ${planPanel.template}
  ${handlersPanel.template}

  <div class="block" id="ds-charts-block">
    <div class="ds-chart-pair">
      <div class="ds-chart-cell">
        <h2>Workload distribution per shift</h2>
        <p class="subdued">% of staff at each workload level today.
          STAFF + ZC merged. The count below each bar is total staff
          present on that shift.</p>
        <div id="ds-workload-dist" class="workload-dist-v"></div>
      </div>
      <div class="ds-chart-cell">
        <h2>Flights distributed per shift</h2>
        <p class="subdued">Share of the day's allocated flights
          picked up by each shift.</p>
        <div id="ds-flights-pie" class="flights-pie"></div>
      </div>
      <div class="ds-side-panels">
        <aside class="ds-chart-cell zc-selector" aria-labelledby="zc-selector-title">
          <h2 id="zc-selector-title">Zone Controllers</h2>
          <p class="subdued">Search the AM List to pick the day's Zone Controllers. The list is saved and carries over to the next day.</p>
          <input id="zc-selector-search" type="search" placeholder="Search person" aria-label="Search AM List">
          <div id="zc-selector-list"></div>
        </aside>
        <aside class="ds-chart-cell roster-edit" aria-labelledby="roster-edit-title">
          <h2 id="roster-edit-title">Roster Change</h2>
          <p class="subdued">Search a staff member and move them to a
            different shift (M / A / N / M1 / A1), or take them Off for
            today. Applies right away.</p>
          <input id="roster-edit-search" type="search" placeholder="Search person" aria-label="Search staff to change">
          <div id="roster-edit-list"></div>
        </aside>
        <aside class="ds-chart-cell handler-assign" aria-labelledby="handler-assign-title">
          <h2 id="handler-assign-title">Handler Assign</h2>
          <p class="subdued">Search a staff member and nominate them as
            the P2F handler for a shift (M / A / N). Applies right away.</p>
          <input id="handler-assign-search" type="search" placeholder="Search person" aria-label="Search staff to nominate">
          <div id="handler-assign-list"></div>
        </aside>
      </div>
    </div>

    <h2 class="block-heading">Shift summary</h2>
    <p class="subdued">Per-shift snapshot — flights, staff, average load,
      INTL count, P2F count, and spread (max-min). Tight spreads
      indicate uniform allocation; wide spreads suggest someone is
      carrying too much or too little.</p>
    <div id="ds-shift-summary"></div>

    <h2 class="block-heading">Flight frequency through the day</h2>
    <p class="subdued">Allocated flights per hour (D-day local). Peaks
      show the rush windows the solver had to clear.</p>
    <div id="ds-flight-trend" class="flight-trend"></div>

    <h2 class="block-heading">Roster by shift</h2>
    <p class="subdued">Today's staff grouped by shift. ZCs listed
      first (green chip), then STAFF. Updates when the date or
      roster changes.</p>
    <div id="ds-roster" class="roster-by-shift"></div>
  </div>

  ${staffingPanel.template}

  <div class="block">
    <h2>Workload review</h2>
    <p class="subdued">Staff with cap-related findings worth a quick check.
      Empty list = clean run.</p>
    <table id="ds-risk-table">
      <thead>
        <tr><th>Name</th><th>Shift</th><th>Actual</th><th>Target</th>
            <th>Cap</th><th>Violation</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="run-status">
    <div class="status-cell"><span class="label">Last run</span>
      <span id="ds-last-run">—</span></div>
    <div class="status-cell"><span class="label">Date</span>
      <span id="ds-run-date">—</span></div>
    <div class="status-cell"><span class="label">Mode</span>
      <span id="ds-mode">—</span></div>
    <div class="status-cell"><span class="label">Solver</span>
      <span id="ds-solver-status" class="badge">—</span></div>
    <div class="status-cell"><span class="label">Duration</span>
      <span id="ds-duration">—</span></div>
  </div>`;

function renderSolverBadge(status) {
  const badge = $("#ds-solver-status");
  badge.textContent = status;
  badge.className = "badge";
  if (GOOD_SOLVER_STATES.includes(status)) return;   // default green badge
  badge.classList.add(status === "—" ? "subdued" : "danger");
}

function renderRiskTable(rows) {
  const tbody = $("#ds-risk-table tbody");
  if (!rows.length) {
    tbody.innerHTML =
      `<tr><td colspan="6" class="subdued">All staff within their caps. Nothing to review. ✓</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td>${escapeHTML(r.name)}</td>
      <td>${escapeHTML(r.shift)}</td>
      <td>${r.actual}</td>
      <td>${r.target_preferred}</td>
      <td>${r.hard_cap}</td>
      <td>${escapeHTML(r.violations) || '<span class="subdued">over cap</span>'}</td>
    </tr>`).join("");
}

export async function load() {
  // The inputs card lives on this panel, so it refreshes with it.
  await Promise.all([store.dashboard.load(), inputsPanel.load()]);
  const d = store.dashboard.data;

  setText("#ds-last-run", d.last_run);
  setText("#ds-run-date", d.run_date);
  setText("#ds-mode", d.mode);
  renderSolverBadge(d.solver_status || "—");
  setText("#ds-duration", d.duration_s == null ? null : `${d.duration_s.toFixed(1)} s`);

  const stats = d.stats || {};
  for (const [elId, key] of Object.entries(HEADLINE_STATS)) {
    setText(`#${elId}`, stats[key]);
  }
  // Keep the tab badge in step so the count is visible from any tab.
  unallocatedView.setPill(Number(stats.flights_unallocated || 0));

  renderRiskTable(d.staff_at_risk || []);

  // Charts render unconditionally. They used to sit inside the at-risk
  // branch, so a clean run silently skipped every chart.
  await renderCharts();
}

/** All five dashboard charts, from one round of fetches.
 *  Reads the dashboard payload the caller already loaded. */
export async function renderCharts() {
  await Promise.all([
    store.workload.load(),
    store.allocations.load({ force: true }),
    store.staffNames.load({ force: true }),
  ]);
  const wlRows = store.workload.data;
  const allocRows = store.allocations.data;
  const names = store.staffNames.data;
  const byStaff = store.shiftByStaff();
  const dDay = store.dashboard.data.run_date || "";

  workloadDist.render(wlRows);
  flightsPie.render(allocRows, byStaff);
  shiftSummary.render(wlRows, allocRows, byStaff);
  flightTrend.render(allocRows, dDay, wlRows);
  rosterChart.render(names);
  await zcSelector.load();
  await rosterEdit.load();
  await handlerAssign.load();
}

export function init(deps) {
  inputsPanel.init(deps);
  zcSelector.init();
  rosterEdit.init();
  handlerAssign.init();
}
