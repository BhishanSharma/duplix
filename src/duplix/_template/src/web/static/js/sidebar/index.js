/* The Setup sidebar — the files that are given once, not every day.
 *
 * The staff roster and the AM + ZC roster are published for a whole
 * period. They used to sit next to the flight schedule on the
 * dashboard, which put three "choose file" slots in front of the
 * operator every morning when only one of them had actually changed.
 * They live here instead: opened from the topbar, filled on the first
 * day of the period, then left alone.
 *
 * Slides in from the right, closes on Escape or an outside click. Also
 * owns the "Add international airport code" form — a once-in-a-while
 * config edit, not a per-run override, so it lives here too.
 */

import { $ } from "../core/dom.js";
import { getOr, post } from "../core/api.js";
import * as slots from "../panels/input-slots.js";
import * as dateControl from "../panels/date-control.js";

/** The kinds this sidebar owns, in the order they're shown. */
const KINDS = ["staff_roster", "am_roster"];

/** IATA codes are exactly three letters. */
const AIRPORT_CODE_LENGTH = 3;

export const template = `
  <aside class="drawer" id="setup-sidebar" hidden
         aria-labelledby="setup-sidebar-title">
    <header>
      <h2 id="setup-sidebar-title">Setup</h2>
      <button class="close-btn" id="close-setup" type="button"
              aria-label="Close">&times;</button>
    </header>
    <div class="drawer-body">
      <section class="setup-section">
        <div class="setup-section-head">
          <h3>Roster files</h3>
          <span id="setup-status" class="inputs-status warn">—</span>
        </div>
        <p class="subdued small">
          Upload each roster when it is published (about once a month).
          They are saved and remembered between runs, and the roster that
          covers the allocation date is picked automatically — a new
          period is simply added alongside the old one.
        </p>
        <div id="setup-coverage" class="roster-coverage" hidden></div>
        <div id="setup-inputs-list" class="inputs-list inputs-list-stacked"></div>
        <p class="subdued small">
          Saved on this computer under <code>data/rosters/</code>. Nothing
          is written back to your files. After adding a roster, re-run
          Plan so the allocation picks it up.
        </p>
      </section>

      <section class="drawer-section" id="setup-intl-airport">
        <h3>Add international airport code</h3>
        <p class="subdued">
          Appends to <code>international_airport_codes</code> in
          <code>configs/config.yml</code>. The new code is picked up on
          the next Plan / Allocate run.
        </p>
        <form id="intl-airport-form" autocomplete="off">
          <label>
            Code
            <input id="intl-airport-code" type="text" maxlength="${AIRPORT_CODE_LENGTH}"
                   placeholder="e.g. BLR" required>
          </label>
          <label>
            Airport name / city (optional)
            <input id="intl-airport-name" type="text" placeholder="e.g. Bengaluru">
          </label>
          <button id="intl-airport-save" class="primary" type="submit">Add</button>
          <span class="intl-airport-status" id="intl-airport-status"></span>
        </form>
      </section>
    </div>
  </aside>`;

export function isOpen() {
  const el = $("#setup-sidebar");
  return !!el && !el.hidden;
}

export function close() {
  $("#setup-sidebar").hidden = true;
}

export async function open() {
  $("#setup-sidebar").hidden = false;
  await slots.load();
}

/** Say so when the allocation date (or the day after it) has no roster
 *  column — the case that used to surface only as an empty plan. */
async function renderCoverage() {
  const box = $("#setup-coverage");
  if (!box) return;
  const day = dateControl.value();
  const cov = await getOr(
    `/api/rosters/coverage${day ? `?date=${encodeURIComponent(day)}` : ""}`,
    null, "rosterCoverage",
  );
  const notices = (cov && cov.notices) || [];
  box.hidden = notices.length === 0;
  box.innerHTML = notices.map((n) => `<p>${n.replace(/</g, "&lt;")}</p>`).join("");
}

/** Render the sidebar's roster-status banner. */
function renderStatus() {
  renderCoverage();
  const missing = slots.groupMissingCount("setup");
  const banner = $("#setup-status");
  if (banner) {
    banner.textContent = missing === 0
      ? "Both rosters saved ✓"
      : missing === 1 ? "1 roster missing" : `${missing} rosters missing`;
    banner.className = "inputs-status " + (missing === 0 ? "ok" : "warn");
  }
}

function setAirportStatus(message, ok) {
  const el = $("#intl-airport-status");
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("ok", !!ok);
  el.classList.toggle("err", !!message && !ok);
}

function initIntlAirportForm() {
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
    if (code.length !== AIRPORT_CODE_LENGTH) {
      setAirportStatus(`Code must be exactly ${AIRPORT_CODE_LENGTH} letters.`, false);
      return;
    }
    setAirportStatus("Saving…", true);
    try {
      await post("/api/intl_airports", { code, name });
      setAirportStatus(`Added ${code} to international list.`, true);
      $("#intl-airport-code").value = "";
      $("#intl-airport-name").value = "";
    } catch (e) {
      setAirportStatus(e.message || "Save failed.", false);
    }
  });
}

export function init() {
  slots.registerMount("#setup-inputs-list", KINDS, renderStatus);
  slots.bindMount("#setup-inputs-list");
  $("#close-setup").addEventListener("click", close);
  initIntlAirportForm();
}
