/* Dashboard control for promoting a rostered person to Zone Controller. */

import { $, escapeHTML } from "../core/dom.js";
import { post } from "../core/api.js";
import * as store from "../core/store.js";

let query = "";
let saving = false;

function isZc(person) {
  return person.is_zc === true || String(person.role || "").toUpperCase() === "ZC"
    || String(person.status || "").toUpperCase().includes("ZC");
}

function source(person) {
  if (isZc(person)) return "ZC";
  return String(person.role || "").toUpperCase() === "AM" ? "AM List" : "Staff";
}

function people() {
  const matches = store.staffNames.data.filter((person) =>
    String(person.name || "").toLowerCase().includes(query));
  return {
    zc: matches.filter(isZc),
    am: matches.filter((person) => !isZc(person) && source(person) === "AM List"),
    staff: matches.filter((person) => !isZc(person) && source(person) === "Staff"),
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

export function render() {
  const host = $("#zc-selector-list");
  if (!host) return;
  const groups = people();
  const badges = groups.zc.length
    ? groups.zc.map((person) => `<span class="zc-selected-badge">${escapeHTML(person.name)} <b>ZC</b></span>`).join("")
    : '<span class="subdued small">No Zone Controllers assigned.</span>';
  const rest = `${section("AM List", groups.am)}${section("Staff", groups.staff)}`;
  host.innerHTML = `<div class="zc-selected">${badges}</div><div class="zc-scroll-list">${rest || '<p class="subdued small">No matching people.</p>'}</div>`;
}

async function promote(name) {
  if (saving) return;
  const date = $("#run-date").value;
  if (!date) return;
  saving = true;
  try {
    await post("/api/overrides", { values: ["change_role", name, "ZC", ""] });
    await post("/api/overrides/apply_staged", { date });
    await store.staffNames.load({ force: true });
    render();
  } catch (error) {
    alert(`Could not assign Zone Controller: ${error.message}`);
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
    const button = event.target.closest(".zc-person");
    if (button) promote(button.dataset.name);
  });
}
