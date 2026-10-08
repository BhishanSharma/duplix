/* Dashboard control for nominating the P2F handler (per shift).
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
import { get, post, del } from "../core/api.js";
import * as store from "../core/store.js";
import * as autocomplete from "../ui/autocomplete.js";
import * as handlersPanel from "./handlers.js";
import * as toast from "../ui/toast.js";

let query = "";
let saving = false;
let roster = [];   // full roster for the run date, from /api/roster_full
let notice = "";
let excludedList = [];   // names marked "not doing P2F today"

const runDate = () => ($("#run-date") || {}).value || "";

/** The choices offered for one person. A P2F handler is only offered
 *  for the person's OWN shift today (M / A / N) and only when the roster
 *  lists them as P2F licensed. Anyone can be taken off P2F for today. */
function roleOptionsFor(person) {
  const opts = [{ value: "", label: "(assign as)" }];
  if (excludedNames().has(String(person.name).trim().toUpperCase())) {
    opts.push({ value: "ALLOW", label: "Allow P2F again" });
    return opts;
  }
  if (person.p2f_licensed && person.assignable
      && ["M", "A", "N"].includes(person.shift)) {
    opts.push({ value: `P2F:${person.shift}`,
                label: `P2F handler — ${person.shift}` });
  }
  opts.push({ value: "EXCLUDE", label: "Not doing P2F today" });
  return opts;
}

/** Upper-cased names currently marked "not doing P2F today". */
function excludedNames() {
  return new Set(excludedList.map((n) => String(n).trim().toUpperCase()));
}

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
  return roles.join(", ");
}

function options(person) {
  return roleOptionsFor(person).map((opt) =>
    `<option value="${opt.value}">${escapeHTML(opt.label)}</option>`
  ).join("");
}

function row(person) {
  const name = escapeHTML(person.name);
  const isExcluded = excludedNames().has(String(person.name).trim().toUpperCase());
  const current = isExcluded ? "Not doing P2F today" : currentRoleLabel(person.name);
  const statusLine = current
    ? `Currently: ${escapeHTML(current)}`
    : escapeHTML(person.shift ? `On ${person.shift}` : (person.status || ""));
  return `
    <div class="handler-assign-row" data-name="${name}">
      <div class="handler-assign-who">
        <span class="handler-assign-name">${name}</span>
        <small class="handler-assign-status">${statusLine}</small>
      </div>
      <select class="handler-assign-select" aria-label="Handler role for ${name}">${options(person)}</select>
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
      excludedList = ((store.handlers.data || {}).p2f_excluded_names) || [];
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
    let label;
    if (roleValue === "EXCLUDE") {
      await post("/api/staged_form/p2f_exclude", { ops_date: date, employee: name });
      notice = `${name} will not be used for P2F today.`;
    } else if (roleValue === "ALLOW") {
      // Remove the p2f_exclude row(s) for this person.
      const ov = await get("/api/overrides");
      const rows = (ov.rows || []).filter((r) =>
        (r.payload.type || "").toLowerCase() === "p2f_exclude"
        && String(r.payload.employee || "").trim().toUpperCase() === name.trim().toUpperCase());
      for (const r of rows.sort((a, b) => b.index - a.index)) {
        await del(`/api/overrides/${r.index}`);
      }
      notice = `${name} can be used for P2F again.`;
    } else {
      const shift = roleValue.split(":")[1];
      const result = await post("/api/staged_form/p2f", {
        ops_date: date, employee: name, shift,
      });
      const ok = (result.applied || {}).p2f > 0 || result.ok;
      label = `P2F handler (${shift})`;
      notice = ok
        ? `${name} nominated as ${label}.`
        : `Saved, but couldn't confirm it took effect for ${name} — check the name and try again.`;
    }
    await store.handlers.load({ force: true });
    handlersPanel.render();
    await load();
  } catch (error) {
    toast.error(`Could not update ${name}: ${error.message}`);
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
