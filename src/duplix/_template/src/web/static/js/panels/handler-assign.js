/* Dashboard control for nominating the P2F handler (per shift) or the
 * NORSE handler.
 *
 * Writes an override row (see write_staged_override) via the
 * staged-form endpoint so it applies immediately — same pattern as
 * the Roster Change pane's change_shift call.
 *
 * Search reads from /api/roster_full (not staffNames) so anyone
 * rostered today can be nominated, not just people already on a
 * visible shift assignment.
 */

import { $, escapeHTML } from "../core/dom.js";
import { get, post } from "../core/api.js";
import * as store from "../core/store.js";
import * as autocomplete from "../ui/autocomplete.js";

let query = "";
let saving = false;
let roster = [];   // full roster for the run date, from /api/roster_full
let notice = "";

const runDate = () => ($("#run-date") || {}).value || "";

/** value = "P2F:<shift>" or "NORSE"; label = what the pane shows. */
const ROLE_OPTIONS = [
  { value: "", label: "(assign as)" },
  { value: "P2F:M", label: "P2F handler — M" },
  { value: "P2F:A", label: "P2F handler — A" },
  { value: "P2F:N", label: "P2F handler — N" },
  { value: "NORSE", label: "NORSE handler" },
];

/** name -> current handler role label(s), from the last /api/handlers
 *  payload (which itself reads override rows + roster shorthand). */
function currentRoleLabel(name) {
  const h = store.handlers.data;
  if (!h) return "";
  const upper = name.trim().toUpperCase();
  const roles = [];
  for (const nom of h.p2f || []) {
    if (nom.name && nom.name.trim().toUpperCase() === upper) {
      roles.push(`P2F — ${nom.shift}`);
    }
  }
  for (const nom of h.norse || []) {
    if (nom.name && nom.name.trim().toUpperCase() === upper) {
      roles.push("NORSE");
    }
  }
  return roles.join(", ");
}

function options() {
  return ROLE_OPTIONS.map((opt) =>
    `<option value="${opt.value}">${escapeHTML(opt.label)}</option>`
  ).join("");
}

function row(person) {
  const name = escapeHTML(person.name);
  const current = currentRoleLabel(person.name);
  const statusLine = current
    ? `Currently: ${escapeHTML(current)}`
    : escapeHTML(person.shift ? `On ${person.shift}` : (person.status || ""));
  return `
    <div class="handler-assign-row" data-name="${name}">
      <div class="handler-assign-who">
        <span class="handler-assign-name">${name}</span>
        <small class="handler-assign-status">${statusLine}</small>
      </div>
      <select class="handler-assign-select" aria-label="Handler role for ${name}">${options()}</select>
      <button class="handler-assign-apply" type="button">Apply</button>
    </div>`;
}

export function render() {
  const host = $("#handler-assign-list");
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
    `<p class="subdued small handler-assign-note">${notice ? `<b>${escapeHTML(notice)}</b>` : "Search a name, pick a role, then Apply."}</p>
     <div class="handler-assign-scroll-list">${list}</div>`;
}

/** Fetch today's full roster and draw. Called on every dashboard load,
 *  so changing the date re-reads the right day. */
export async function load() {
  const date = runDate();
  if (date) {
    try {
      const [payload] = await Promise.all([
        get("/api/roster_full"),
        store.handlers.load(),   // so "Currently: ..." reflects the latest
      ]);
      roster = payload.names || [];
    } catch (error) {
      console.warn("roster_full load failed (handler-assign)", error);
      roster = [];
    }
  } else {
    roster = [];
  }
  notice = "";
  render();
}

async function apply(name, roleValue) {
  if (saving || !roleValue) return;
  const date = runDate();
  if (!date) return;
  saving = true;
  try {
    let result, ok, label;
    if (roleValue === "NORSE") {
      result = await post("/api/staged_form/norse", {
        ops_date: date, employee: name,
      });
      ok = (result.applied || {}).norse > 0 || result.ok;
      label = "NORSE handler";
    } else {
      const shift = roleValue.split(":")[1];
      result = await post("/api/staged_form/p2f", {
        ops_date: date, employee: name, shift,
      });
      ok = (result.applied || {}).p2f > 0 || result.ok;
      label = `P2F handler (${shift})`;
    }
    notice = ok
      ? `${name} nominated as ${label}.`
      : `Saved, but couldn't confirm it took effect for ${name} — check the name and try again.`;
    // The nomination lives in override rows, read fresh by /api/handlers —
    // reload it so the new "Currently: ..." tag shows right away.
    await store.handlers.load({ force: true });
    await load();
  } catch (error) {
    alert(`Could not nominate ${name}: ${error.message}`);
  } finally {
    saving = false;
  }
}

export function init() {
  $("#handler-assign-search").addEventListener("input", (event) => {
    query = event.target.value.trim().toLowerCase();
    render();
  });
  autocomplete.attach($("#handler-assign-search"), {
    getCandidates: (q) => autocomplete.rank(roster, q)
      .map((p) => ({ label: p.name, sublabel: currentRoleLabel(p.name) || p.shift || "" })),
  });
  $("#handler-assign-list").addEventListener("click", (event) => {
    const btn = event.target.closest(".handler-assign-apply");
    if (!btn) return;
    const rowEl = btn.closest(".handler-assign-row");
    const select = rowEl.querySelector(".handler-assign-select");
    apply(rowEl.dataset.name, select.value);
  });
}
