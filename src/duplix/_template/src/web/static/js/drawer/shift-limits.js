/* Drawer form: edit the preferred / acceptable / hard-cap band for one
 * (shift, role) bucket.
 *
 * The override lasts for this iteration only — Reset restores the
 * configured defaults.
 */

import { $ } from "../core/dom.js";
import { post } from "../core/api.js";
import { SHIFT_OPTIONS } from "../core/constants.js";
import * as store from "../core/store.js";

export const template = `
  <section class="drawer-section" id="drawer-shift-limits">
    <h3>Edit shift limits</h3>
    <p class="subdued">
      Per-iteration override of preferred / acceptable / hard-cap for a
      (shift, role) bucket. Defaults restore on Reset.
    </p>
    <form id="shift-limits-form" autocomplete="off">
      <label>
        Shift
        <select id="sl-shift">
          <option value="">(choose)</option>
          ${SHIFT_OPTIONS.map((s) => `<option>${s}</option>`).join("")}
        </select>
      </label>
      <label>
        Role
        <select id="sl-role">
          <option value="">(choose)</option>
          <option value="STAFF">Normal</option>
          <option value="ZC">Zone Controller</option>
        </select>
      </label>
      <div class="sl-current subdued" id="sl-current">Pick a shift + role to see current values.</div>
      <label>Min (preferred) <input id="sl-min" type="number" min="0" step="1"></label>
      <label>Target (acceptable max) <input id="sl-target" type="number" min="0" step="1"></label>
      <label>Max (hard cap) <input id="sl-max" type="number" min="0" step="1"></label>
      <button id="sl-save" class="primary" type="submit">Save</button>
      <button id="sl-cancel" class="ghost" type="button">Cancel</button>
      <span class="intl-airport-status" id="sl-status"></span>
    </form>
  </section>`;

export async function load() {
  await store.shiftLimits.load();
  refreshHint();
}

/** Show the band currently in force for the picked (shift, role) —
 *  merged defaults plus any override already saved. */
function refreshHint() {
  const shift = $("#sl-shift").value;
  const role = $("#sl-role").value;
  const hint = $("#sl-current");
  if (!hint) return;
  if (!shift || !role) {
    hint.textContent = "Pick a shift + role to see current values.";
    return;
  }
  const band = (store.shiftLimits.data.bands[shift] || {})[role];
  if (!band) {
    hint.textContent = `No band found for ${shift}/${role}.`;
    return;
  }
  hint.textContent =
    `Current ${shift}/${role}: min=${band.min}, target=${band.target}, max=${band.max}`;
  $("#sl-min").placeholder = String(band.min);
  $("#sl-target").placeholder = String(band.target);
  $("#sl-max").placeholder = String(band.max);
}

function setStatus(message, ok) {
  const el = $("#sl-status");
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("ok", !!ok);
  el.classList.toggle("err", !!message && !ok);
}

/** Returns an error message, or null when the values are usable. */
function validate({ shift, role, min, target, max }) {
  if (!shift || !role) return "Pick a shift and a role.";
  if (![min, target, max].every(Number.isInteger)) {
    return "min, target, max must be whole numbers.";
  }
  if (min <= 0 || target <= 0 || max <= 0) return "Values must be positive.";
  if (!(min <= target && target <= max)) return "Require min <= target <= max.";
  return null;
}

export function init() {
  const form = $("#shift-limits-form");
  if (!form) return;

  $("#sl-shift").addEventListener("change", refreshHint);
  $("#sl-role").addEventListener("change", refreshHint);
  $("#sl-cancel").addEventListener("click", () => {
    $("#sl-min").value = "";
    $("#sl-target").value = "";
    $("#sl-max").value = "";
    setStatus("");
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const values = {
      shift: $("#sl-shift").value,
      role: $("#sl-role").value,
      min: Number($("#sl-min").value),
      target: Number($("#sl-target").value),
      max: Number($("#sl-max").value),
    };
    const problem = validate(values);
    if (problem) {
      setStatus(problem, false);
      return;
    }
    setStatus("Saving…", true);
    try {
      await post("/api/shift_limits", values);
      setStatus(
        `Saved ${values.shift}/${values.role}. Active for this iteration; Reset reverts.`,
        true,
      );
      await load();
    } catch (e) {
      setStatus(e.message || "Save failed.", false);
    }
  });
}
