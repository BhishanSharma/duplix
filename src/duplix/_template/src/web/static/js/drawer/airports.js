/* Drawer form: add an international airport code.
 *
 * Posts to /api/intl_airports, which validates and appends to
 * configs/config.yml. The engine picks the new code up on the next
 * Plan / Allocate.
 */

import { $ } from "../core/dom.js";
import { post } from "../core/api.js";

/** IATA codes are exactly three letters. */
const CODE_LENGTH = 3;

export const template = `
  <section class="drawer-section" id="drawer-intl-airport">
    <h3>Add international airport code</h3>
    <p class="subdued">
      Appends to <code>international_airport_codes</code> in
      <code>configs/config.yml</code>. The new code is picked up on the
      next Plan / Allocate run.
    </p>
    <form id="intl-airport-form" autocomplete="off">
      <label>
        Code
        <input id="intl-airport-code" type="text" maxlength="${CODE_LENGTH}"
               placeholder="e.g. BLR" required>
      </label>
      <label>
        Airport name / city (optional)
        <input id="intl-airport-name" type="text" placeholder="e.g. Bengaluru">
      </label>
      <button id="intl-airport-save" class="primary" type="submit">Add</button>
      <span class="intl-airport-status" id="intl-airport-status"></span>
    </form>
  </section>`;

function setStatus(message, ok) {
  const el = $("#intl-airport-status");
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("ok", !!ok);
  el.classList.toggle("err", !!message && !ok);
}

export function init() {
  const form = $("#intl-airport-form");
  if (!form) return;

  // Force uppercase and strip non-letters as the operator types, so
  // case is never something they have to think about.
  $("#intl-airport-code").addEventListener("input", (ev) => {
    const cleaned = ev.target.value.replace(/[^A-Za-z]/g, "").toUpperCase();
    if (cleaned !== ev.target.value) ev.target.value = cleaned;
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const code = ($("#intl-airport-code").value || "").trim().toUpperCase();
    const name = ($("#intl-airport-name").value || "").trim();
    if (code.length !== CODE_LENGTH) {
      setStatus(`Code must be exactly ${CODE_LENGTH} letters.`, false);
      return;
    }
    setStatus("Saving…", true);
    try {
      await post("/api/intl_airports", { code, name });
      setStatus(`Added ${code} to international list.`, true);
      $("#intl-airport-code").value = "";
      $("#intl-airport-name").value = "";
    } catch (e) {
      setStatus(e.message || "Save failed.", false);
    }
  });
}
