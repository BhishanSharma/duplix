/* Dashboard control for moving an EXISTING staff member to a different
 * shift, or taking them off, for the day.
 *
 * Search reads from /api/roster_full rather than the staffNames
 * resource: staffNames only lists people currently on a shift, so an
 * assigner who takes someone off could never find them again to put
 * them back. roster_full always lists everyone rostered today,
 * whatever their current status.
 *
 * Note: this rewrites the person's status outright. Using it on a ZC
 * or a P2F handler (status like M/ZC or M/P2F) also clears that role,
 * same as it would if you retyped their roster cell by hand.
 */

import { $, escapeHTML } from "../core/dom.js";
import { get, post } from "../core/api.js";
import * as store from "../core/store.js";
import * as rosterChart from "../charts/roster.js";
import * as autocomplete from "../ui/autocomplete.js";
import * as toast from "../ui/toast.js";

let query = "";
let saving = false;
let roster = [];   // full roster for the run date, from /api/roster_full
let notice = "";

const runDate = () => ($("#run-date") || {}).value || "";

// value = the CrewStatus literal the backend expects in the "shift"
// override column; label = what the pane shows. Off is the only
// off/leave option — the reason (leave vs day-off) isn't tracked here.
const SHIFT_OPTIONS = [
  { value: "M", label: "M" },
  { value: "A", label: "A" },
  { value: "N", label: "N" },
  { value: "M1", label: "M1" },
  { value: "A1", label: "A1" },
  { value: "F", label: "Off" },
];

const OFF_LEAVE_LABELS = {
  "F": "Off", "P/L": "On leave", "C/L": "On leave (casual)", "C/OFF": "Off",
};

/** What best matches one of SHIFT_OPTIONS for this person right now —
 *  their plain shift, or their off/leave status. */
function currentValue(person) {
  const status = String(person.status || "").toUpperCase();
  if (status in OFF_LEAVE_LABELS) return status;
  return person.shift || "";
}

function statusLabel(person) {
  const status = String(person.status || "").toUpperCase();
  return OFF_LEAVE_LABELS[status] || person.shift || status || "—";
}

function options(person) {
  const current = currentValue(person);
  return SHIFT_OPTIONS.map((opt) =>
    `<option value="${opt.value}"${opt.value === current ? " selected" : ""}>${opt.label}</option>`
  ).join("");
}

function row(person) {
  const name = escapeHTML(person.name);
  const roleNote = /ZC|P2F/.test(String(person.status || "").toUpperCase())
    ? ` <small class="roster-edit-warn" title="Changing shift here also clears ${name}'s ZC/P2F role">⚠</small>` : "";
  return `
    <div class="roster-edit-row" data-name="${name}">
      <div class="roster-edit-who">
        <span class="roster-edit-name">${name}</span>
        <small class="roster-edit-status">${escapeHTML(statusLabel(person))}</small>${roleNote}
      </div>
      <select class="roster-edit-select" aria-label="New shift for ${name}">${options(person)}</select>
      <button class="roster-edit-apply" type="button">Apply</button>
    </div>`;
}

export function render() {
  const host = $("#roster-edit-list");
  if (!host) return;
  if (!roster.length) {
    host.innerHTML = `<p class="subdued small">Run Plan to load the roster, then search a name.</p>`;
    return;
  }
  const matches = query
    ? roster.filter((p) => String(p.name || "").toLowerCase().includes(query))
    : roster;
  const list = matches.slice(0, 60).map(row).join("")
    || `<p class="subdued small">No matching people.</p>`;
  host.innerHTML =
    `<p class="subdued small roster-edit-note">${notice ? `<b>${escapeHTML(notice)}</b>` : "Search a name, pick a shift or Off, then Apply."}</p>
     <div class="roster-edit-scroll-list">${list}</div>`;
}

/** Fetch today's full roster and draw. Called on every dashboard load,
 *  so changing the date re-reads the right day. */
export async function load() {
  const date = runDate();
  if (date) {
    try {
      const payload = await get("/api/roster_full");
      roster = payload.names || [];
    } catch (error) {
      console.warn("roster_full load failed", error);
      roster = [];
    }
  } else {
    roster = [];
  }
  notice = "";
  render();
}

async function apply(name, newShift) {
  if (saving || !newShift) return;
  const date = runDate();
  if (!date) return;
  saving = true;
  try {
    const result = await post("/api/staged_form/change_shift", {
      ops_date: date, employee: name, shift: newShift,
    });
    const ok = (result.applied || {}).change_shift > 0;
    notice = ok
      ? `${name} moved to ${OFF_LEAVE_LABELS[newShift] || newShift}.`
      : `Saved, but couldn't confirm it took effect for ${name} — check the name and try again.`;
    await load();
    await store.staffNames.load({ force: true });
    rosterChart.render(store.staffNames.data);
  } catch (error) {
    toast.error(`Could not change ${name}'s shift: ${error.message}`);
  } finally {
    saving = false;
  }
}

export function init() {
  $("#roster-edit-search").addEventListener("input", (event) => {
    query = event.target.value.trim().toLowerCase();
    render();
  });
  autocomplete.attach($("#roster-edit-search"), {
    getCandidates: (q) => autocomplete.rank(roster, q)
      .map((p) => ({ label: p.name, sublabel: statusLabel(p) })),
  });
  $("#roster-edit-list").addEventListener("click", (event) => {
    const btn = event.target.closest(".roster-edit-apply");
    if (!btn) return;
    const rowEl = btn.closest(".roster-edit-row");
    const select = rowEl.querySelector(".roster-edit-select");
    apply(rowEl.dataset.name, select.value);
  });
}
