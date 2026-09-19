/* The tab strip and the panels behind it.
 *
 * Views are registered in one list; the strip, the panel elements and
 * the switch-to-tab dispatch are all derived from it. Adding a tab is
 * adding a module to views/index.js — nothing here changes.
 */

import { $, $$ } from "../core/dom.js";
import { VIEWS } from "../views/index.js";

const DEFAULT_TAB = VIEWS[0].id;

/** The tab strip markup, built from the view registry. */
export function stripTemplate() {
  return VIEWS.map((view, i) => {
    const pill = view.pillId
      ? ` <span class="tab-pill" id="${view.pillId}" hidden>0</span>`
      : "";
    return `<button class="tab${i === 0 ? " active" : ""}" data-tab="${view.id}">${view.label}${pill}</button>`;
  }).join("\n      ");
}

/** One <section> per view, each holding that view's own markup. */
export function panelsTemplate() {
  return VIEWS.map((view, i) => `
    <section class="panel" id="panel-${view.id}"${i === 0 ? "" : " hidden"}>
      ${view.template}
    </section>`).join("\n");
}

/** The view the strip currently marks active. */
export function activeTab() {
  return $$(".tab.active")[0]?.dataset.tab || DEFAULT_TAB;
}

/** Show one panel and load its data. */
export function switchTab(name) {
  for (const view of VIEWS) {
    const panel = $(`#panel-${view.id}`);
    if (panel) panel.hidden = view.id !== name;
  }
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  return loadTab(name);
}

/** Load a view's data without touching which panel is visible — used
 *  after a run or a date change to refresh whatever is on screen. */
export function loadTab(name) {
  const view = VIEWS.find((v) => v.id === name) || VIEWS[0];
  return view.load();
}

export function init(deps) {
  for (const view of VIEWS) view.init(deps);
  $$(".tab").forEach((b) =>
    b.addEventListener("click", () => switchTab(b.dataset.tab)));
}
