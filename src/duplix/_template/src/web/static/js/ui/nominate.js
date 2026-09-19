/* The "nominate P2F / NORSE handlers" prompt shown after a Plan that
 * left a handler slot empty. Dismissing it suppresses it until the next
 * Plan — it's a nudge, not a blocker.
 */

import { $ } from "../core/dom.js";
import * as store from "../core/store.js";

export const template = `
  <div class="modal-backdrop" id="nominate-modal" hidden>
    <div class="modal">
      <header>
        <h2 id="nominate-title">Nominate handlers</h2>
        <button class="close-btn" id="nominate-close" type="button" aria-label="Close">&times;</button>
      </header>
      <div class="modal-body">
        <p id="nominate-message">—</p>
        <p class="subdued">Use the <strong>Override</strong> drawer to add
          a P2F or NORSE nomination row, then click <strong>Allocate</strong>.</p>
      </div>
      <div class="modal-actions">
        <button id="nominate-open-override" class="primary" type="button">Open Override drawer</button>
        <button id="nominate-dismiss" class="ghost" type="button">Dismiss</button>
      </div>
    </div>
  </div>`;

let suppressed = false;

/** Re-arm the popup — called when a new Plan starts. */
export function rearm() {
  suppressed = false;
}

export function isOpen() {
  const el = $("#nominate-modal");
  return !!el && !el.hidden;
}

export function close() {
  $("#nominate-modal").hidden = true;
  suppressed = true;   // don't re-open until next Plan
}

function messageFor(h) {
  const missing = h.missing_p2f_shifts || [];
  const noNorse = !h.norse_nominated;
  if (missing.length && noNorse) {
    return `No P2F handlers nominated for shifts ${missing.join(", ")},
      and no NORSE handler. Mark a STAFF row as M/P2F (or A/P2F, N/P2F) in the
      roster, or add a row in the Override drawer. NORSE has no roster shorthand —
      add it via Override.`;
  }
  if (missing.length) {
    return `No P2F handlers nominated for shift(s) ${missing.join(", ")}.
      Mark a STAFF row as ${missing[0]}/P2F (or any other missing shift)
      in IN_Staff, or add a row in the Override drawer.`;
  }
  return `P2F handlers are nominated. Now add a NORSE handler via the
    Override drawer (NORSE has no roster shorthand).`;
}

/** Show the prompt if a handler slot is empty and it hasn't already
 *  been dismissed since the last Plan. */
export function maybeShow() {
  if (suppressed) return;
  const h = store.handlers.data;
  if (!h) return;
  const missingP2F = (h.missing_p2f_shifts || []).length > 0;
  const noNorse = !h.norse_nominated;
  if (!missingP2F && !noNorse) return;
  $("#nominate-title").textContent = noNorse && !missingP2F
    ? "Nominate NORSE handler"
    : "Nominate P2F and NORSE handlers";
  $("#nominate-message").textContent = messageFor(h);
  $("#nominate-modal").hidden = false;
}

export function init({ onOpenOverride }) {
  $("#nominate-close").addEventListener("click", close);
  $("#nominate-dismiss").addEventListener("click", close);
  $("#nominate-open-override").addEventListener("click", () => {
    close();
    onOpenOverride();
  });
}
