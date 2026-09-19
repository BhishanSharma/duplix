/* Unallocated tab — the exception panel.
 *
 * A count card up top, the list below, and a green "all allocated"
 * banner when there's nothing to show. Each row carries up to three
 * ranked override suggestions; clicking one stages it, and the sticky
 * banner applies the staged set and re-solves.
 */

import { $, escapeHTML } from "../core/dom.js";
import { api, post } from "../core/api.js";
import * as store from "../core/store.js";

export const id = "unallocated";
export const label = "Unallocated";
/** Rendered in the tab strip next to the label. */
export const pillId = "tab-unalloc-pill";

/** Suggestion type -> the payload fields the apply POST needs.
 *  Data-driven so a fifth override type is one entry, not a branch. */
const RECO_ROUTES = {
  waive_h10_pair:    ["flight", "std", "employee", "other_std"],
  raise_cap:         ["flight", "std", "employee"],
  skip_p2f_buffer:   ["flight", "std", "employee"],
  skip_intl_removal: ["flight", "std"],
};

/** Short chip label per override type — has to fit in the cell. */
const RECO_LABEL = {
  waive_h10_pair:    "Waive H10",
  raise_cap:         "Raise cap",
  skip_p2f_buffer:   "Skip P2F buf",
  skip_intl_removal: "Skip INTL rmv",
};

export const template = `
  <div class="unalloc-header">
    <div class="unalloc-count-card" id="unalloc-count-card">
      <div class="unalloc-count" id="unalloc-count">—</div>
      <div class="unalloc-label">Unallocated flights</div>
    </div>
    <div class="unalloc-empty" id="unalloc-empty" hidden>
      <strong>All flights allocated.</strong>
      <span class="subdued">Every flight found a slot.</span>
    </div>
    <div class="unalloc-bulk-apply">
      <button id="unalloc-apply-all" class="primary" type="button">
        Apply all recommendations
      </button>
      <span id="unalloc-apply-all-status" class="subdued"></span>
    </div>
  </div>
  <div class="reco-banner" id="reco-banner" hidden></div>
  <div class="scroll-wrap" id="unalloc-list-wrap">
    <table id="unalloc-table">
      <thead>
        <tr>
          <th>Flt</th><th>STD</th><th>Dep</th><th>Arr</th>
          <th>Pax</th><th>Class</th><th>Intl</th><th>Reason</th>
          <th>Recommender</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>`;

/** Staged selections, keyed by flight + type + target staff so the
 *  three "Waive H10 → A / → B / → C" chips on one flight toggle
 *  independently. Cleared on refresh and after a successful apply. */
const staged = new Map();

const stageKey = (flightId, kind, target) => `${flightId}|${kind}|${target || ""}`;

/** Set by init() so the banner can kick off a re-solve without this
 *  module importing the run panel (which imports the views). */
let triggerRun = () => {};

export async function load() {
  await Promise.all([
    store.unallocated.load(),
    store.recommendations.load(),
  ]);
  render();
}

function recoCell(rec) {
  if (!rec || !rec.suggestions || rec.suggestions.length === 0) {
    return `<span class="subdued">no fix found</span>`;
  }
  return rec.suggestions.map((s) => {
    const target = s.target_staff || "";
    const who = target ? ` → ${escapeHTML(target)}` : "";
    const selected = staged.has(stageKey(rec.flight_id, s.type, target));
    const tick = selected ? '<span class="reco-tick">✓</span>' : "";
    return `
      <div class="reco-row">
        <button class="btn btn-xs reco-propose${selected ? " selected" : ""}"
                data-flight-id="${escapeHTML(rec.flight_id)}"
                data-kind="${escapeHTML(s.type)}"
                data-target="${escapeHTML(target)}"
                data-payload="${encodeURIComponent(JSON.stringify(s.payload || {}))}"
                title="${escapeHTML(s.rationale || "")}">
          ${tick}${escapeHTML(RECO_LABEL[s.type] || s.type)}${who}
        </button>
      </div>`;
  }).join("");
}

/** INTL = soft pink; ferry/test = light blue. */
function depArrClass(row) {
  if (row.is_international) return "cell-intl";
  const cls = String(row.ops_class || "").toLowerCase();
  return cls === "test" || cls === "ferry" ? "cell-special" : "";
}

export function render() {
  const payload = store.unallocated.data;
  const count = payload.count || 0;
  const rows = payload.rows || [];

  $("#unalloc-count").textContent = count;
  // Card warning tint only when there's something to act on — clean
  // dashboards stay neutral.
  $("#unalloc-count-card").classList.toggle("warn", count > 0);
  $("#unalloc-empty").hidden = count > 0;
  $("#unalloc-list-wrap").hidden = count === 0;
  setPill(count);

  // Index recommendations by (flt, std) so each row is a lookup rather
  // than a scan of the whole list.
  const recByKey = {};
  for (const r of store.recommendations.data.recommendations || []) {
    recByKey[`${r.flt}|${r.std}`] = r;
  }

  $("#unalloc-table tbody").innerHTML = rows.map((r) => {
    const cellClass = depArrClass(r);
    return `
      <tr>
        <td>${escapeHTML(r.flt)}</td>
        <td>${escapeHTML(r.std)}</td>
        <td class="${cellClass}">${escapeHTML(r.dep)}</td>
        <td class="${cellClass}">${escapeHTML(r.arr)}</td>
        <td>${escapeHTML(r.pax)}</td>
        <td>${escapeHTML(r.ops_class)}</td>
        <td>${r.is_international ? "yes" : ""}</td>
        <td>${escapeHTML(r.reason)}</td>
        <td class="reco-cell">${recoCell(recByKey[`${r.flt}|${r.std}`])}</td>
      </tr>`;
  }).join("");

  renderBanner();
}

/** Tab-pill badge, so the count is visible without switching tabs. */
export function setPill(count) {
  const pill = $(`#${pillId}`);
  if (!pill) return;
  pill.textContent = count;
  pill.hidden = Number(count) === 0;
}

function renderBanner() {
  const banner = $("#reco-banner");
  if (!banner) return;
  if (staged.size === 0) {
    banner.hidden = true;
    banner.innerHTML = "";
    return;
  }
  banner.hidden = false;
  banner.innerHTML = `
    <span class="reco-banner-count">${staged.size} override(s) selected</span>
    <button id="reco-clear-btn" class="btn btn-xs">Clear</button>
    <button id="reco-reallocate-btn" class="btn btn-xs btn-primary">
      Apply &amp; Reallocate
    </button>`;
  $("#reco-clear-btn").addEventListener("click", () => {
    staged.clear();
    render();
  });
  $("#reco-reallocate-btn").addEventListener("click", applyStagedAndReallocate);
}

function toggleStaged(flightId, kind, payload, target) {
  const key = stageKey(flightId, kind, target);
  if (staged.has(key)) staged.delete(key);
  else staged.set(key, { flight_id: flightId, kind, payload, target });
  render();   // cheap; refreshes ticks + banner
}

/** Write one staged suggestion as an override row. The solver only
 *  reads the override list, so there's no separate approval step. */
async function applyOne(item) {
  const fields = RECO_ROUTES[item.kind];
  if (!fields) {
    console.warn("unknown reco kind", item.kind);
    return false;
  }
  const payload = {};
  for (const f of fields) {
    if (item.payload[f] !== undefined && item.payload[f] !== null) {
      payload[f] = String(item.payload[f]);
    }
  }
  try {
    await post("/api/recommender/apply", { kind: item.kind, payload });
    return true;
  } catch (e) {
    console.warn(`reco apply failed (${item.kind})`, e);
    return false;
  }
}

async function applyStagedAndReallocate() {
  if (staged.size === 0) return;
  const btn = $("#reco-reallocate-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Applying overrides…"; }
  let ok = 0;
  let fail = 0;
  for (const item of staged.values()) {
    if (await applyOne(item)) ok++;
    else fail++;
  }
  if (fail > 0) {
    alert(`${ok} override(s) applied; ${fail} failed (see console). Re-allocating anyway.`);
  }
  staged.clear();
  // The run status pill takes over from here.
  await triggerRun();
  if (btn) { btn.disabled = false; btn.textContent = "Apply & Reallocate"; }
}

async function applyAllRecommendations() {
  const btn = $("#unalloc-apply-all");
  const status = $("#unalloc-apply-all-status");
  btn.disabled = true;
  if (status) { status.textContent = "Applying recommendations…"; status.style.color = ""; }
  try {
    const resp = await api("/api/recommender/apply_all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (status) {
      status.textContent =
        `Done — ${resp.applied}/${resp.total} applied, ${resp.failed} failed. `
        + "Click Allocate to solve with the new waivers.";
      status.style.color = resp.failed ? "#8a1f1f" : "#0a5d0a";
      status.style.fontWeight = "bold";
    }
    // The new rows land in the drawer, which re-reads them when opened.
    store.invalidate("overrides");
  } catch (e) {
    if (status) {
      status.textContent = `Failed: ${e.message}`;
      status.style.color = "#8a1f1f";
    }
  } finally {
    btn.disabled = false;
  }
}

export function init(deps) {
  triggerRun = deps.triggerRun;

  // Delegated so re-rendering the table doesn't need re-wiring.
  $("#unalloc-table").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button.reco-propose");
    if (!btn) return;
    let payload = {};
    try {
      payload = JSON.parse(decodeURIComponent(btn.dataset.payload || "{}"));
    } catch (e) {
      console.warn("bad payload on reco button", e);
    }
    toggleStaged(btn.dataset.flightId, btn.dataset.kind, payload, btn.dataset.target || "");
  });

  $("#unalloc-apply-all").addEventListener("click", applyAllRecommendations);
}
