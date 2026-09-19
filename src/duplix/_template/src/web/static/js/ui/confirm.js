/* The in-app confirm dialog.
 *
 * Replaces window.confirm(), which renders with a "127.0.0.1:8765 says"
 * prefix that reads like a browser security warning.
 */

import { $ } from "../core/dom.js";

export const template = `
  <div class="modal-backdrop" id="confirm-modal" hidden>
    <div class="modal">
      <header>
        <h2 id="confirm-title">Are you sure?</h2>
        <button class="close-btn" id="confirm-close" type="button" aria-label="Close">&times;</button>
      </header>
      <div class="modal-body">
        <p id="confirm-message">—</p>
      </div>
      <div class="modal-actions">
        <button id="confirm-cancel" class="ghost" type="button">Cancel</button>
        <button id="confirm-ok" class="primary" type="button">Continue</button>
      </div>
    </div>
  </div>`;

let resolver = null;

/** Ask for confirmation. Resolves true (Continue) or false (Cancel /
 *  Close / Escape). One prompt at a time: a second call while one is
 *  open declines the first rather than deadlocking the UI. */
export function confirmModal(title, message, okLabel) {
  return new Promise((resolve) => {
    if (resolver) { resolver(false); resolver = null; }
    $("#confirm-title").textContent = title || "Are you sure?";
    $("#confirm-message").textContent = message || "";
    $("#confirm-ok").textContent = okLabel || "Continue";
    $("#confirm-modal").hidden = false;
    resolver = resolve;
  });
}

export function isOpen() {
  const el = $("#confirm-modal");
  return !!el && !el.hidden;
}

export function resolve(value) {
  $("#confirm-modal").hidden = true;
  if (resolver) {
    const r = resolver;
    resolver = null;
    r(value);
  }
}

export function init() {
  $("#confirm-ok").addEventListener("click", () => resolve(true));
  $("#confirm-cancel").addEventListener("click", () => resolve(false));
  $("#confirm-close").addEventListener("click", () => resolve(false));
  // Click on the backdrop (but NOT on the modal box itself) → cancel.
  $("#confirm-modal").addEventListener("click", (ev) => {
    if (ev.target === $("#confirm-modal")) resolve(false);
  });
}
