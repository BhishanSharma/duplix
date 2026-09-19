/* The Setup sidebar — the files that are given once, not every day.
 *
 * The staff roster and the AM + ZC roster are published for a whole
 * period. They used to sit next to the flight schedule on the
 * dashboard, which put three "choose file" slots in front of the
 * operator every morning when only one of them had actually changed.
 * They live here instead: opened from the topbar, filled on the first
 * day of the period, then left alone.
 *
 * Same shell as the Override drawer (slides from the right, closes on
 * Escape or an outside click) but a separate surface, because these are
 * session inputs rather than per-run edits.
 */

import { $ } from "../core/dom.js";
import * as slots from "../panels/input-slots.js";

/** The kinds this sidebar owns, in the order they're shown. */
const KINDS = ["staff_roster", "am_roster"];

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
          Set these once. Both rosters cover a whole period, so they only
          need replacing when a new one is published — not with every
          day's allocation.
        </p>
        <div id="setup-inputs-list" class="inputs-list inputs-list-stacked"></div>
        <p class="subdued small">
          Held in the server's memory for this session. Nothing is written
          back to the files. After replacing one, re-run Plan so the
          allocation picks up the new roster.
        </p>
      </section>

      <section class="setup-section">
        <h3>Today's flight schedule</h3>
        <p class="subdued small">
          Not here on purpose — the schedule is a fresh export every
          morning, so it's on the dashboard where the day starts.
        </p>
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

/** Render the sidebar's roster-status banner. */
function renderStatus() {
  const missing = slots.groupMissingCount("setup");
  const banner = $("#setup-status");
  if (banner) {
    banner.textContent = missing === 0
      ? "Both rosters loaded ✓"
      : missing === 1 ? "1 roster missing" : `${missing} rosters missing`;
    banner.className = "inputs-status " + (missing === 0 ? "ok" : "warn");
  }
}

export function init() {
  slots.registerMount("#setup-inputs-list", KINDS, renderStatus);
  slots.bindMount("#setup-inputs-list");
  $("#close-setup").addEventListener("click", close);
}
