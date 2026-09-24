/* Builds the page out of the pieces each module owns.
 *
 * index.html is a shell with a few empty mount points. Every view and
 * panel brings its own markup, so a new tab or a new setup form is one
 * module — index.html never grows.
 *
 * Mounting happens before any init(), so every listener can assume its
 * elements exist.
 */

import { $ } from "../core/dom.js";
import * as tabs from "./tabs.js";
import * as confirmDialog from "./confirm.js";
import * as setup from "../sidebar/index.js";

export function mount() {
  $("#tab-strip").innerHTML = tabs.stripTemplate();
  $("#panels").innerHTML = tabs.panelsTemplate();
  $("#overlays").innerHTML = setup.template + confirmDialog.template;
}

/** Escape hatches, so a render bug can never trap the operator behind
 *  an open sidebar or modal. Innermost layer closes first. */
function onEscape(ev) {
  if (ev.key !== "Escape") return;
  if (confirmDialog.isOpen()) confirmDialog.resolve(false);
  else if (setup.isOpen()) setup.close();
}

/** Enter accepts the confirm dialog. */
function onEnter(ev) {
  if (ev.key === "Enter" && confirmDialog.isOpen()) confirmDialog.resolve(true);
}

/* A click outside the Setup sidebar closes it — except while a modal is
 * layered over it. */
function onDocumentClick(ev) {
  if (confirmDialog.isOpen()) return;
  if ($("#confirm-modal")?.contains(ev.target)) return;

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
