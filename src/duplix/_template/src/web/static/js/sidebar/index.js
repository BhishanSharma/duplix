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
 * owns the international airport code list (view / add / remove), the
 * shift break length and the per-staff flight caps — once-in-a-while
 * config edits, not per-run overrides, so they live here too.
 */

import { $, escapeHTML } from "../core/dom.js";
import { getOr, post } from "../core/api.js";
import { confirmModal } from "../ui/confirm.js";
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
        <div class="drawer-section-head">
          <h3>International airport codes</h3>
          <span class="subdued" id="intl-airport-count"></span>
        </div>
        <p class="subdued">
          Flights departing from these airports are treated as
          international. Saved to <code>international_airport_codes</code>
          in <code>configs/config.yml</code>; changes are picked up on the
          next Plan / Allocate run.
        </p>
        <ul class="intl-airport-list" id="intl-airport-list"
            aria-label="International airport codes"></ul>
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
          <span class="form-status" id="intl-airport-status"></span>
        </form>
      </section>

      <section class="drawer-section" id="setup-break-length">
        <div class="drawer-section-head">
          <h3>Shift break length</h3>
          <span class="subdued" id="break-length-current"></span>
        </div>
        <p class="subdued">
          Every on-shift staff member gets one flight-free break of this
          length, kept out of the first and last hour of their shift.
          Saved to <code>break_pass.length_minutes</code> in
          <code>configs/config.yml</code>; takes effect on the next
          Allocate run.
        </p>
        <form id="break-length-form" autocomplete="off" novalidate>
          <label>
            <span>Minutes <span id="break-length-range"></span></span>
            <input id="break-length-input" type="number" inputmode="numeric"
                   step="1" required>
          </label>
          <button id="break-length-save" class="primary" type="submit">Save</button>
          <span class="form-status" id="break-length-status"></span>
        </form>
      </section>

      <section class="drawer-section" id="setup-hard-caps">
        <div class="drawer-section-head">
          <h3>Max flights per staff</h3>
        </div>
        <p class="subdued">
          The most flights one person can be given in a shift — the
          allocator never goes over it. A cap below the preferred or
          target count lowers those to match. Saved to
          <code>configs/shift_limits.json</code>; takes effect on the
          next Allocate run.
        </p>
        <form id="hard-caps-form" autocomplete="off" novalidate>
          <div class="hard-caps-grid" id="hard-caps-grid"></div>
          <p class="hard-caps-override" id="hard-caps-override" hidden></p>
          <button id="hard-caps-save" class="primary" type="submit">Save</button>
          <span class="form-status" id="hard-caps-status"></span>
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
  await Promise.all([
    slots.load(), loadAirports(), loadBreakLength(), loadHardCaps(),
  ]);
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

function setStatus(selector, message, ok) {
  const el = $(selector);
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("ok", !!ok);
  el.classList.toggle("err", !!message && !ok);
}

/** Airport chips, alphabetical so a code is easy to find. */
function renderAirports(airports) {
  const list = $("#intl-airport-list");
  if (!list) return;
  const sorted = [...airports].sort((a, b) => a.code.localeCompare(b.code));
  const count = $("#intl-airport-count");
  if (count) {
    count.textContent = sorted.length === 1 ? "1 code" : `${sorted.length} codes`;
  }
  if (!sorted.length) {
    list.innerHTML = '<li class="subdued">No international airports configured.</li>';
    return;
  }
  list.innerHTML = sorted.map(({ code, name }) => {
    const c = escapeHTML(code);
    const label = name ? `${c} (${escapeHTML(name)})` : c;
    return `
      <li class="intl-chip"${name ? ` title="${escapeHTML(name)}"` : ""}>
        <span class="intl-chip-code">${c}</span>
        ${name ? `<span class="intl-chip-name">${escapeHTML(name)}</span>` : ""}
        <button class="intl-chip-remove" type="button" data-code="${c}"
                aria-label="Remove ${label}" title="Remove ${label}">&times;</button>
      </li>`;
  }).join("");
}

async function loadAirports() {
  const payload = await getOr("/api/intl_airports", null, "intlAirports");
  if (payload) renderAirports(payload.airports || []);
}

async function removeAirport(btn) {
  const code = btn.dataset.code;
  const ok = await confirmModal(
    `Remove ${code}?`,
    `Flights departing ${code} will be treated as domestic from the next `
      + "Plan / Allocate run. You can add the code back at any time.",
    "Remove",
  );
  if (!ok) return;
  btn.disabled = true;
  setAirportStatus(`Removing ${code}…`, true);
  try {
    const res = await post("/api/intl_airports/remove", { code });
    renderAirports(res.airports || []);
    setAirportStatus(`Removed ${code} from the international list.`, true);
  } catch (e) {
    btn.disabled = false;
    setAirportStatus(e.message || "Remove failed.", false);
  }
}

const setAirportStatus = (message, ok) =>
  setStatus("#intl-airport-status", message, ok);
const setBreakStatus = (message, ok) =>
  setStatus("#break-length-status", message, ok);

/** Allowed range for the break length, from the server. */
let breakRange = null;

function renderBreakLength(payload) {
  breakRange = { min: payload.min, max: payload.max };
  const input = $("#break-length-input");
  input.min = payload.min;
  input.max = payload.max;
  input.value = payload.length_minutes;
  $("#break-length-range").textContent = `(${payload.min}–${payload.max})`;
  $("#break-length-current").textContent = `Currently ${payload.length_minutes} min`;
}

async function loadBreakLength() {
  const payload = await getOr("/api/break_length", null, "breakLength");
  if (payload) renderBreakLength(payload);
}

function initBreakLengthForm() {
  const form = $("#break-length-form");
  if (!form) return;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const raw = ($("#break-length-input").value || "").trim();
    const minutes = Number(raw);
    if (!raw || !Number.isInteger(minutes)) {
      setBreakStatus("Enter a whole number of minutes.", false);
      return;
    }
    if (breakRange && (minutes < breakRange.min || minutes > breakRange.max)) {
      setBreakStatus(
        `Break length must be between ${breakRange.min} and ${breakRange.max} minutes.`,
        false,
      );
      return;
    }
    const btn = $("#break-length-save");
    btn.disabled = true;
    setBreakStatus("Saving…", true);
    try {
      const res = await post("/api/break_length", { length_minutes: minutes });
      renderBreakLength(res);
      setBreakStatus(
        `Saved. Breaks will be ${res.length_minutes} min from the next Allocate run.`,
        true,
      );
    } catch (e) {
      setBreakStatus(e.message || "Save failed.", false);
    } finally {
      btn.disabled = false;
    }
  });
}

const setCapsStatus = (message, ok) =>
  setStatus("#hard-caps-status", message, ok);

/** The caps as last loaded or saved, `{shift: {role: cap}}`. */
let savedCaps = {};

/** One row per shift, one number input per role. A cell shadowed by a
 *  per-run shift-limits override is flagged, since the next run uses
 *  the override's cap rather than the saved one until Reset. */
function renderHardCaps(payload) {
  savedCaps = payload.caps || {};
  const overrides = payload.overrides || {};
  const shifts = Object.keys(savedCaps);
  const roles = [...new Set(shifts.flatMap((s) => Object.keys(savedCaps[s])))];
  const head = roles.map((r) => `<th scope="col">${escapeHTML(r)}</th>`).join("");
  const body = shifts.map((shift) => {
    const cells = roles.map((role) => {
      const cap = savedCaps[shift][role];
      if (cap === undefined) return "<td></td>";
      const over = overrides[`${shift}/${role}`];
      const flag = over === undefined ? "" : ` class="overridden"
        title="This run uses ${over} (per-run override) until Reset"`;
      return `
        <td><input type="number" inputmode="numeric" min="1" step="1"
                   value="${cap}" data-shift="${escapeHTML(shift)}"
                   data-role="${escapeHTML(role)}"${flag}
                   aria-label="${escapeHTML(`${shift} ${role} max flights`)}"></td>`;
    }).join("");
    return `<tr><th scope="row">${escapeHTML(shift)}</th>${cells}</tr>`;
  }).join("");
  $("#hard-caps-grid").innerHTML = `
    <table class="hard-caps-table">
      <thead><tr><th scope="col">Shift</th>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table>`;

  const note = $("#hard-caps-override");
  const active = Object.entries(overrides);
  note.hidden = active.length === 0;
  note.textContent = active.length
    ? "Per-run override active until Reset: "
      + active.map(([key, cap]) => `${key} uses ${cap}`).join(", ") + "."
    : "";
}

async function loadHardCaps() {
  const payload = await getOr("/api/hard_caps", null, "hardCaps");
  if (payload) renderHardCaps(payload);
}

function initHardCapsForm() {
  const form = $("#hard-caps-form");
  if (!form) return;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const caps = {};
    let changed = 0;
    for (const input of form.querySelectorAll("input[data-shift]")) {
      const { shift, role } = input.dataset;
      const raw = (input.value || "").trim();
      const cap = Number(raw);
      if (!raw || !Number.isInteger(cap) || cap < 1) {
        setCapsStatus(`${shift} ${role}: enter a whole number of 1 or more.`, false);
        input.focus();
        return;
      }
      (caps[shift] ||= {})[role] = cap;
      if (savedCaps[shift]?.[role] !== cap) changed += 1;
    }
    if (!changed) {
      setCapsStatus("Nothing changed.", true);
      return;
    }
    const btn = $("#hard-caps-save");
    btn.disabled = true;
    setCapsStatus("Saving…", true);
    try {
      const res = await post("/api/hard_caps", { caps });
      renderHardCaps(res);
      const notes = res.notes || [];
      setCapsStatus(
        "Saved. Used from the next Allocate run."
          + (notes.length ? ` ${notes.join("; ")}.` : ""),
        true,
      );
    } catch (e) {
      setCapsStatus(e.message || "Save failed.", false);
    } finally {
      btn.disabled = false;
    }
  });
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

  // Delegated, so a re-render keeps working.
  $("#intl-airport-list").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".intl-chip-remove");
    if (btn && !btn.disabled) removeAirport(btn);
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
      const res = await post("/api/intl_airports", { code, name });
      if (res.airports) renderAirports(res.airports);
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
  initBreakLengthForm();
  initHardCapsForm();
}
