/* The Override drawer's row table.
 *
 * Each row replays through the solver on the next run. Which columns a
 * row type actually uses comes from the backend
 * (/api/override_types), so adding a type to the schema enum is enough
 * — the dropdown and the per-type column visibility follow.
 */

import { $, $$, escapeHTML } from "../core/dom.js";
import { post, put, del } from "../core/api.js";
import { ROLE_OPTIONS, SHIFT_OPTIONS } from "../core/constants.js";
import * as store from "../core/store.js";
import { confirmModal } from "../ui/confirm.js";

export const template = `
  <p class="subdued">
    Each row replays through the solver on the next run. Rows live in
    this session — Reset keeps them, restarting the server clears them.
  </p>
  <div class="scroll-wrap">
    <table id="override-table">
      <thead><tr id="override-thead-row"><th>Actions</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="drawer-actions">
    <button id="override-add-row" class="primary" type="button">Add row</button>
    <button id="override-save" class="ghost" type="button">Save (no re-run)</button>
    <button id="override-run" class="ghost" type="button">Save &amp; re-run</button>
  </div>`;

/** Used until /api/override_types resolves. The canonical list lives in
 *  the backend's schemas.OVERRIDE_TYPES_FOR_UI. */
let typeOptions = [
  "p2f", "norse", "sick", "change_role", "max_flights", "cutoff_time",
];

/** type -> the column names that type cares about, lowercased. Empty
 *  until the fetch resolves, which reads as "show everything". */
let relevantCols = {};

/** Minimal fallback when the server sends no column list. Without it,
 *  "Add row" posts an empty array and the table renders as a lone
 *  Actions column. */
const DEFAULT_HEADERS = ["type", "employee", "shift", "limit"];

let headers = [];
let rows = [];

/** How long a save flash stays on screen. */
const FLASH_OK_MS = 2000;
const FLASH_ERR_MS = 3000;

/** Set by init() so this module doesn't import the run/drawer panels. */
let deps = {};

export async function loadTypes() {
  const data = await store.overrideTypes.load();
  if (Array.isArray(data?.types) && data.types.length) typeOptions = data.types;
  if (data?.relevant_cols && typeof data.relevant_cols === "object") {
    relevantCols = data.relevant_cols;
  }
}

export async function load() {
  const data = await store.overrides.load();
  headers = data.headers || [];
  rows = data.rows || [];
  if (headers.length === 0) {
    console.warn("no override columns from server — using defaults");
    headers = DEFAULT_HEADERS.slice();
  }
  render();
}

/** Map a header label to the editor it needs. Case-insensitive and
 *  tolerant of upstream column reordering. */
function fieldKind(header) {
  const h = String(header || "").trim().toLowerCase();
  if (h === "type") return "type";
  if (h === "employee" || h === "name") return "employee";
  if (h === "shift") return "shift";
  if (h === "limit") return "limit";
  return "other";
}

/** Should this column be editable for a row of this type? A false here
 *  renders a grey "—" instead, so a p2f row doesn't show empty std /
 *  other_std / dep / arr cells alongside its real ones. */
function columnRelevant(headerName, rowType) {
  const tp = String(rowType || "").trim().toLowerCase();
  const hdr = String(headerName || "").trim().toLowerCase();
  // No type picked yet: only the type column is live, which forces the
  // assigner to choose one first.
  if (!tp) return hdr === "type";
  const allowed = relevantCols[tp];
  // No list pushed for this type — show everything rather than risk
  // hiding a cell that matters.
  if (!Array.isArray(allowed) || allowed.length === 0) return true;
  return allowed.includes(hdr);
}

function selectCell(colIdx, kind, options, value, placeholder) {
  const opts = ["", ...options].map((o) =>
    `<option value="${escapeHTML(o)}"${o === value ? " selected" : ""}>${escapeHTML(o || placeholder)}</option>`
  ).join("");
  return `<td><select data-col="${colIdx}" data-kind="${kind}">${opts}</select></td>`;
}

function renderCell(kind, colIdx, value, rowType, headerName) {
  // The type column is always editable — it's how the row's behaviour
  // gets picked. Hidden cells keep their value in data-val so a save
  // round-trip preserves whatever was there before the type changed.
  if (kind !== "type" && !columnRelevant(headerName, rowType)) {
    return `<td class="cell-disabled" data-col="${colIdx}" data-val="${escapeHTML(value || "")}">
              <span class="subdued">—</span>
            </td>`;
  }
  if (kind === "type") {
    return selectCell(colIdx, "type", typeOptions, value, "(choose)");
  }
  if (kind === "shift") {
    // For a change_role row the "shift" column carries the new ROLE
    // instead of a shift code, so the option list swaps.
    const isRoleRow = String(rowType || "").trim().toLowerCase() === "change_role";
    return selectCell(
      colIdx, "shift",
      isRoleRow ? ROLE_OPTIONS : SHIFT_OPTIONS,
      value,
      isRoleRow ? "(choose new role)" : "(any)",
    );
  }
  if (kind === "employee") {
    return selectCell(
      colIdx, "employee", store.staffNames.data.map((s) => s.name),
      value, "(choose name)",
    );
  }
  // limit (max_flights / cutoff_time) and any unknown column: free text.
  const hint = kind === "limit" ? "e.g. 18 or 14:30" : "";
  return `<td>
    <input type="text" data-col="${colIdx}" value="${escapeHTML(value || "")}" placeholder="${hint}">
  </td>`;
}

export function render() {
  const headRow = $("#override-thead-row");
  const tbody = $("#override-table tbody");
  if (!headRow || !tbody) return;

  headRow.innerHTML = "<th>Actions</th>"
    + headers.map((h) => `<th>${escapeHTML(h)}</th>`).join("");

  const kinds = headers.map(fieldKind);
  const typeColIdx = kinds.indexOf("type");

  tbody.innerHTML = rows.map((r) => {
    const rowType = typeColIdx >= 0 ? r.values[typeColIdx] : "";
    const cells = headers.map((h, i) =>
      renderCell(kinds[i], i, r.values[i] || "", rowType, h)).join("");
    return `
      <tr data-row="${r.index}">
        <td class="row-actions">
          <button class="ghost btn-save" type="button">Modify</button>
          <button class="ghost btn-del" type="button">Delete</button>
        </td>
        ${cells}
      </tr>`;
  }).join("");
}

/** Read a row in column order, from whichever editor each cell uses.
 *  Disabled cells return their stashed value, so switching a row from
 *  waive_h10_pair to p2f hides std / other_std but a save still writes
 *  what they held. */
function readRowValues(tr) {
  const cells = $$("[data-col]", tr);
  cells.sort((a, b) => parseInt(a.dataset.col, 10) - parseInt(b.dataset.col, 10));
  return cells.map((el) => (el.tagName === "TD" ? el.dataset.val || "" : el.value));
}

/** Flash a row + its button so a save is impossible to miss. */
function flash(tr, btn, { label, bg, rowBg, ms }) {
  const original = btn.textContent;
  btn.textContent = label;
  btn.style.background = bg;
  btn.style.color = "white";
  btn.style.fontWeight = "bold";
  tr.style.background = rowBg;
  tr.style.transition = "background 0.3s";
  setTimeout(() => {
    btn.textContent = original;
    btn.style.background = "";
    btn.style.color = "";
    btn.style.fontWeight = "";
    tr.style.background = "";
  }, ms);
}

async function modifyRow(tr, btn) {
  const rowIdx = parseInt(tr.dataset.row, 10);
  try {
    // Send the current ops date so the server re-applies staged-eligible
    // rows (change_role, p2f, sick, ...) to the data sheets right away,
    // not just at the next Plan / Allocate.
    await put(`/api/overrides/${rowIdx}`, {
      values: readRowValues(tr),
      date: deps.currentDate(),
    });
    flash(tr, btn, {
      label: "✓ Saved", bg: "#28a745", rowBg: "#d4edda", ms: FLASH_OK_MS,
    });
    await deps.refreshDashboard();
  } catch (e) {
    console.error("[override] save failed:", e);
    flash(tr, btn, {
      label: "✗ Failed", bg: "#dc3545", rowBg: "#f8d7da", ms: FLASH_ERR_MS,
    });
    alert(`Save failed: ${e.message}`);
  }
}

async function deleteRow(rowIdx) {
  const ok = await confirmModal(
    "Delete override row",
    "This removes the override row. You can re-add it any time.",
    "Delete",
  );
  if (!ok) return;
  try {
    await del(`/api/overrides/${rowIdx}`);
    await load();
  } catch (e) {
    alert(`Delete failed: ${e.message}`);
  }
}

async function addRow() {
  try {
    await post("/api/overrides", { values: headers.map(() => "") });
    await load();
  } catch (e) {
    console.error("[override] add row failed:", e);
    alert(`Add row failed: ${e.message}`);
  }
}

/** PUT every visible row. Returns how many failed, so both Save and
 *  Save & re-run can decide what to do about it. */
export async function saveAllRows() {
  let failed = 0;
  for (const tr of $$("#override-table tbody tr[data-row]")) {
    const rowIdx = parseInt(tr.dataset.row, 10);
    if (isNaN(rowIdx)) continue;
    const values = readRowValues(tr);
    if (!values.some((v) => v && String(v).trim())) continue;
    try {
      await put(`/api/overrides/${rowIdx}`, { values });
    } catch (e) {
      console.warn(`row ${rowIdx} save failed`, e);
      failed++;
    }
  }
  return failed;
}

/** When the Type dropdown changes, persist what's been typed so far and
 *  re-render, so the per-type column visibility follows. Delegated, so
 *  no per-row wiring. */
function onTypeChanged(ev) {
  const sel = ev.target;
  if (sel.tagName !== "SELECT" || sel.dataset.kind !== "type") return;
  const tr = sel.closest("tr");
  if (!tr || !tr.dataset.row) return;
  const cacheRow = rows.find((r) => r.index === parseInt(tr.dataset.row, 10));
  if (cacheRow) cacheRow.values = readRowValues(tr);
  render();
}

export function init(injected) {
  deps = injected;
  const table = $("#override-table");
  table.addEventListener("change", onTypeChanged);
  table.addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr");
    if (!tr || !tr.dataset.row) return;
    if (ev.target.classList.contains("btn-save")) {
      modifyRow(tr, ev.target);
    } else if (ev.target.classList.contains("btn-del")) {
      deleteRow(parseInt(tr.dataset.row, 10));
    }
  });
  $("#override-add-row").addEventListener("click", addRow);
}
