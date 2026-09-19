/* Warnings tab — severity-badged engine warnings. */

import { $, escapeHTML } from "../core/dom.js";
import * as store from "../core/store.js";

export const id = "warnings";
export const label = "Warnings";

export const template = `
  <div class="filters">
    <label>Severity
      <select id="warn-filter-sev">
        <option value="">all</option>
        <option>ERROR</option><option>WARN</option><option>INFO</option>
      </select>
    </label>
    <label>Search
      <input type="search" id="warn-filter-q" placeholder="code / message / name">
    </label>
    <span class="count" id="warn-count">0 warnings</span>
  </div>
  <div class="scroll-wrap">
    <table id="warn-table">
      <thead>
        <tr><th>Severity</th><th>Code</th><th>Date</th><th>Name</th><th>Message</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>`;

function severityBadge(sev) {
  const cls = sev === "ERROR" ? "danger" : sev === "WARN" ? "warn" : "subdued";
  return `<span class="badge ${cls}">${escapeHTML(sev || "—")}</span>`;
}

export async function load() {
  await store.warnings.load();
  render();
}

export function render() {
  const sev = $("#warn-filter-sev").value;
  const q = $("#warn-filter-q").value.trim().toUpperCase();
  const tbody = $("#warn-table tbody");
  const matched = [];

  for (const r of store.warnings.data) {
    if (sev && r.severity !== sev) continue;
    if (q) {
      const blob = `${r.code} ${r.message} ${r.name}`.toUpperCase();
      if (!blob.includes(q)) continue;
    }
    matched.push(`
      <tr>
        <td>${severityBadge(r.severity)}</td>
        <td><code>${escapeHTML(r.code)}</code></td>
        <td>${escapeHTML(r.date)}</td>
        <td>${escapeHTML(r.name)}</td>
        <td>${escapeHTML(r.message)}</td>
      </tr>`);
  }

  tbody.innerHTML = matched.length
    ? matched.join("")
    : `<tr><td colspan="5" class="subdued">No matching warnings.</td></tr>`;
  $("#warn-count").textContent = `${matched.length} warnings`;
}

export function init() {
  $("#warn-filter-sev").addEventListener("change", render);
  $("#warn-filter-q").addEventListener("input", render);
}
