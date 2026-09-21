/* Dashboard control for choosing the day's Zone Controllers.
 *
 * The rosters no longer carry /ZC, so this panel is where ZCs come from.
 * The list is saved per date on the server and carried over to the next
 * day until it is edited: open tomorrow, see today's ZCs pre-filled, add
 * or remove whoever changed. Anyone on the roster can be picked — AM List
 * and Staff are searched together.
 */

import { $, escapeHTML } from "../core/dom.js";
import { get, post } from "../core/api.js";
import * as store from "../core/store.js";
import * as rosterChart from "../charts/roster.js";

let query = "";
let saving = false;
let list = { date: "", names: [], source: "none", carried_from: null };
let notice = "";

const norm = (s) => String(s || "").replace(/\s+/g, " ").trim().toUpperCase();
const runDate = () => ($("#run-date") || {}).value || "";

function isZc(person) {
  return person.is_zc === true || String(person.role || "").toUpperCase() === "ZC"
    || String(person.status || "").toUpperCase().includes("ZC");
}

function group(person) {
  return String(person.role || "").toUpperCase() === "AM" ? "AM List" : "Staff";
}

function candidates() {
  const matches = store.staffNames.data.filter((person) =>
    !isZc(person) && String(person.name || "").toLowerCase().includes(query));
  return {
    am: matches.filter((person) => group(person) === "AM List"),
    staff: matches.filter((person) => group(person) === "Staff"),
  };
}

function section(title, entries) {
  if (!entries.length) return "";
  return `<section class="zc-list-section"><h3>${title}</h3>${entries.map((person) => {
    const name = escapeHTML(person.name || "");
    const shift = escapeHTML(person.shift || "No shift");
    return `<button class="zc-person" type="button" data-name="${name}" title="Make ${name} a Zone Controller">
      <span>${name}</span><small>${shift}</small></button>`;
  }).join("")}</section>`;
}

function badge(name, { removable, muted = "", title = "" }) {
  const n = escapeHTML(name);
  const cls = muted ? "zc-selected-badge zc-badge-muted" : "zc-selected-badge";
  const tag = muted ? `<small>${escapeHTML(muted)}</small>` : "<b>ZC</b>";
  const x = removable
    ? `<button class="zc-remove" type="button" data-name="${n}" aria-label="Remove ${n} as Zone Controller" title="Remove ${n}">×</button>`
    : "";
  return `<span class="${cls}"${title ? ` title="${escapeHTML(title)}"` : ""}>${n} ${tag}${x}</span>`;
}

function sourceNote() {
  const d = list.date;
  if (list.source === "carried") {
    return `Carried over from ${escapeHTML(list.carried_from)}. Add or remove people to save a list for ${escapeHTML(d)}.`;
  }
  if (list.source === "saved") return `Saved for ${escapeHTML(d)}. It carries over to the next day.`;
  return "No list saved yet — pick the Zone Controllers below.";
}

export function render() {
  const host = $("#zc-selector-list");
  if (!host) return;
  const roster = store.staffNames.data;
  const rosterLoaded = roster.length > 0;
  const inList = new Set(list.names.map(norm));

  // Current ZCs on today's roster, whatever the search box says.
  const onRoster = roster.filter(isZc);
  const shown = new Set(onRoster.map((p) => norm(p.name)));
  const rosterNames = new Set(roster.map((p) => norm(p.name)));

  // Every ZC is removable, including one marked /ZC in the roster file.
  const badges = onRoster.map((person) => badge(person.name, {
    removable: true,
    title: inList.has(norm(person.name)) ? "" : "Marked ZC in the roster file",
  }));
  // Listed people who are not (yet) ZC on today's roster.
  for (const name of list.names) {
    if (shown.has(norm(name))) continue;
    const muted = rosterLoaded && !rosterNames.has(norm(name)) ? "not on shift" : "applies on Plan";
    badges.push(badge(name, { removable: true, muted }));
  }

  const groups = candidates();
  const rest = `${section("AM List", groups.am)}${section("Staff", groups.staff)}`;
  const empty = !rosterLoaded
    ? "Run Plan to load the roster, then pick Zone Controllers."
    : "No matching people.";

  host.innerHTML = `
    <div class="zc-selected">${badges.join("") || '<span class="subdued small">No Zone Controllers assigned.</span>'}</div>
    <p class="subdued small zc-note">${sourceNote()}${notice ? ` <b>${escapeHTML(notice)}</b>` : ""}</p>
    <div class="zc-scroll-list">${rest || `<p class="subdued small">${empty}</p>`}</div>`;
}

/** Fetch the saved list for the date on screen, then draw. Called with
 *  every dashboard load, so changing the date re-reads its own list. */
export async function load() {
  const date = runDate();
  if (date) {
    try {
      list = await get(`/api/zc?date=${encodeURIComponent(date)}`);
    } catch (error) {
      console.warn("zc list load failed", error);
    }
  }
  notice = "";
  render();
}

async function change(action, name) {
  if (saving) return;
  const date = runDate();
  if (!date) return;
  saving = true;
  try {
    const result = await post(`/api/zc/${action}`, { date, name });
    list = result;
    // "not planned yet" is normal (list saved, applies on Plan); anything
    // else means the roster could not be re-read and the operator should know.
    notice = result.applied === false && result.reason !== "not planned yet"
      ? `Saved, but not applied: ${result.reason}` : "";
    await store.staffNames.load({ force: true });
    render();
    rosterChart.render(store.staffNames.data);
  } catch (error) {
    alert(`Could not update Zone Controllers: ${error.message}`);
  } finally {
    saving = false;
  }
}

export function init() {
  $("#zc-selector-search").addEventListener("input", (event) => {
    query = event.target.value.trim().toLowerCase();
    render();
  });
  $("#zc-selector-list").addEventListener("click", (event) => {
    const remove = event.target.closest(".zc-remove");
    if (remove) return change("remove", remove.dataset.name);
    const add = event.target.closest(".zc-person");
    if (add) return change("add", add.dataset.name);
  });
}
