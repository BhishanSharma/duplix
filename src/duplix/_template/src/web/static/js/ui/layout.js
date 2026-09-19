/* Builds the page out of the pieces each module owns.
 *
 * index.html is a shell with four empty mount points. Every view, panel
 * and drawer section brings its own markup, so a new tab or a new
 * drawer form is one module — index.html never grows.
 *
 * Mounting happens before any init(), so every listener can assume its
 * elements exist.
 */

import { $ } from "../core/dom.js";
import * as tabs from "./tabs.js";
import * as confirmDialog from "./confirm.js";
import * as nominate from "./nominate.js";
import * as drawer from "../drawer/index.js";
import * as setup from "../sidebar/index.js";

export function mount() {
  $("#tab-strip").innerHTML = tabs.stripTemplate();
  $("#panels").innerHTML = tabs.panelsTemplate();
  $("#overlays").innerHTML =
    drawer.template + setup.template
    + confirmDialog.template + nominate.template;
}

/** Escape hatches, so a render bug can never trap the operator behind
 *  an open drawer or modal. Innermost layer closes first. */
function onEscape(ev) {
  if (ev.key !== "Escape") return;
  if (confirmDialog.isOpen()) confirmDialog.resolve(false);
  else if (nominate.isOpen()) nominate.close();
  else if (drawer.isOpen()) drawer.close();
  else if (setup.isOpen()) setup.close();
}

/** Enter accepts the confirm dialog. */
function onEnter(ev) {
  if (ev.key === "Enter" && confirmDialog.isOpen()) confirmDialog.resolve(true);
}

/* A click outside the drawer closes it — except for the two buttons
 * that legitimately open it (their click bubbles here and would
 * auto-close what they just opened), and except while a modal is
 * layered over it. The modals' buttons live outside #override-drawer,
 * so a "Delete" click was bubbling through and closing the drawer right
 * after a row deletion. */
const DRAWER_SAFE_TRIGGERS = new Set(["open-override", "nominate-open-override"]);

function onDocumentClick(ev) {
  if (confirmDialog.isOpen() || nominate.isOpen()) return;
  if ($("#confirm-modal")?.contains(ev.target)) return;
  if ($("#nominate-modal")?.contains(ev.target)) return;

  if (drawer.isOpen()
      && !$("#override-drawer").contains(ev.target)
      && !DRAWER_SAFE_TRIGGERS.has(ev.target.id)) {
    drawer.close();
  }
  if (setup.isOpen()
      && !$("#setup-sidebar").contains(ev.target)
      && !ev.target.closest?.("#open-setup, #inputs-open-setup")) {
    setup.close();
  }
}

export function initGlobalHandlers() {
  document.addEventListener("keydown", onEscape);
  document.addEventListener("keydown", onEnter);
  document.addEventListener("click", onDocumentClick);
}
