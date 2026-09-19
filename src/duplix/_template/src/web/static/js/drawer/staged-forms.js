/* Drawer forms that add or remove a flight or a staff member.
 *
 * Each form posts to /api/staged_form/<op>, which writes an override
 * row AND applies the change to the data sheets immediately — so the
 * dashboard shows the projected state on the next refresh rather than
 * waiting for the next Plan.
 *
 * The four forms differ only in their fields and their validation, so
 * they're declared as data and built from one renderer.
 */

import { $ } from "../core/dom.js";
import { post } from "../core/api.js";
import { SHIFT_OPTIONS } from "../core/constants.js";
import * as rows from "./rows.js";

const OK_COLOR = "#0a5d0a";
const ERR_COLOR = "#8a1f1f";

const shiftSelect = (id, placeholder, required) => ({
  id, type: "select", label: "Shift", required,
  placeholder,
  options: SHIFT_OPTIONS,
});

/** Each form: the op it posts to, its fields, how to build the payload,
 *  and what it refuses to submit without. */
const FORMS = [
  {
    op: "add_flight",
    id: "add-flight-form",
    section: "drawer-add-flight",
    heading: "Add flight",
    blurb: `Adds a flight to the cleaned list. Picked up by the next
      Plan / Allocate. Routes by ops class (day / night / p2f /
      norse / ferry / charter / test / gulf).`,
    submitLabel: "Add flight",
    statusId: "af-status",
    fields: [
      { id: "af-date", type: "date", label: "Date", required: true },
      { id: "af-flt", type: "text", label: "Flight no.", required: true },
      { id: "af-type", type: "text", label: "TYPE", placeholder: "J, H, B, K, X...", maxlength: 3 },
      { id: "af-ac", type: "text", label: "A/C type (model)", placeholder: "320, 321, 789..." },
      { id: "af-dep", type: "text", label: "DEP", maxlength: 3, required: true },
      { id: "af-arr", type: "text", label: "ARR", maxlength: 3, required: true },
      { id: "af-std", type: "time", label: "STD", required: true },
      { id: "af-pax", type: "number", label: "Pax", min: 0, value: 0 },
      { id: "af-owner", type: "text", label: "Aircraft Owner", placeholder: "(blank if none)" },
      {
        id: "af-ops-class", type: "select", label: "Ops class", required: true,
        options: ["day", "night", "p2f", "norse", "ferry", "charter", "test", "gulf"],
        plain: true,
      },
    ],
    payload: () => ({
      date: $("#af-date").value,
      flight: $("#af-flt").value.trim(),
      ac_type: $("#af-type").value.trim().toUpperCase(),
      ac: $("#af-ac").value.trim(),
      dep: $("#af-dep").value.trim().toUpperCase(),
      arr: $("#af-arr").value.trim().toUpperCase(),
      std: $("#af-std").value,
      pax: $("#af-pax").value || "0",
      owner: $("#af-owner").value.trim(),
      ops_class: $("#af-ops-class").value,
    }),
    validate: (p) => (p.flight && p.dep && p.arr && p.std)
      ? null : "Flight no., DEP, ARR, and STD are required.",
  },
  {
    op: "remove_flight",
    id: "remove-flight-form",
    section: "drawer-remove-flight",
    heading: "Remove flight",
    blurb: `Removes a flight from the cleaned list. Match by flight no +
      STD + DEP + ARR + date (MM/DD).`,
    submitLabel: "Remove flight",
    statusId: "rf-status",
    fields: [
      { id: "rf-date", type: "text", label: "Date (MM/DD)", placeholder: "05/27", required: true },
      { id: "rf-flt", type: "text", label: "Flight no.", required: true },
      { id: "rf-std", type: "time", label: "STD", required: true },
      { id: "rf-dep", type: "text", label: "DEP", maxlength: 3, required: true },
      { id: "rf-arr", type: "text", label: "ARR", maxlength: 3, required: true },
    ],
    payload: () => ({
      date: $("#rf-date").value.trim(),      // MM/DD, per the match rule
      flight: $("#rf-flt").value.trim(),
      std: $("#rf-std").value,
      dep: $("#rf-dep").value.trim().toUpperCase(),
      arr: $("#rf-arr").value.trim().toUpperCase(),
    }),
    validate: (p) => (p.flight && p.std)
      ? null : "Flight no. and STD are required.",
  },
  {
    op: "add_staff",
    id: "add-staff-form",
    section: "drawer-add-staff",
    heading: "Add staff",
    blurb: "Adds a staff entry to today's roster (assignable=True).",
    submitLabel: "Add staff",
    statusId: "as-status",
    fields: [
      { id: "as-name", type: "text", label: "Name", required: true },
      shiftSelect("as-shift", "(choose)", true),
    ],
    payload: () => ({
      employee: $("#as-name").value.trim(),
      shift: $("#as-shift").value,
    }),
    validate: (p) => (p.employee && p.shift)
      ? null : "Name and shift are required.",
  },
  {
    op: "remove_staff",
    id: "remove-staff-form",
    section: "drawer-remove-staff",
    heading: "Remove staff",
    blurb: "Marks the named staff as un-assignable for today.",
    submitLabel: "Remove staff",
    statusId: "rs-status",
    fields: [
      { id: "rs-name", type: "text", label: "Name", required: true },
      shiftSelect("rs-shift", "(any)", false),
    ],
    payload: () => ({
      employee: $("#rs-name").value.trim(),
      shift: $("#rs-shift").value || "",
    }),
    validate: (p) => (p.employee ? null : "Name is required."),
  },
];

function fieldHtml(f) {
  if (f.type === "select") {
    const head = f.plain ? "" : `<option value="">${f.placeholder}</option>`;
    const opts = f.options.map((o) => `<option value="${o}">${o}</option>`).join("");
    const label = f.plain ? f.label : `${f.label}${f.required ? "" : " (optional)"}`;
    return `<label>${label}
      <select id="${f.id}"${f.required ? " required" : ""}>${head}${opts}</select>
    </label>`;
  }
  const attrs = [
    `id="${f.id}"`,
    `type="${f.type}"`,
    f.placeholder ? `placeholder="${f.placeholder}"` : "",
    f.maxlength ? `maxlength="${f.maxlength}"` : "",
    f.min !== undefined ? `min="${f.min}"` : "",
    f.value !== undefined ? `value="${f.value}"` : "",
    f.required ? "required" : "",
  ].filter(Boolean).join(" ");
  return `<label>${f.label} <input ${attrs}></label>`;
}

export const template = FORMS.map((form) => `
  <section class="drawer-section" id="${form.section}">
    <h3>${form.heading}</h3>
    <p class="subdued">${form.blurb}</p>
    <form id="${form.id}" autocomplete="off">
      ${form.fields.map(fieldHtml).join("\n      ")}
      <button class="primary" type="submit">${form.submitLabel}</button>
      <span class="staged-form-status" id="${form.statusId}"></span>
    </form>
  </section>`).join("\n");

function setStatus(statusId, message, ok) {
  const el = $(`#${statusId}`);
  if (!el) return;
  el.textContent = message;
  el.style.color = ok ? OK_COLOR : ERR_COLOR;
  el.style.fontWeight = "bold";
}

/** Set by init() so this module doesn't import the drawer or the date
 *  control (both of which reach back into it). */
let deps = {};

async function submit(form, formEl) {
  const dateIso = deps.currentDate();
  if (!dateIso) {
    setStatus(form.statusId, "Pick an operating date first.", false);
    return;
  }
  const payload = form.payload();
  const problem = form.validate(payload);
  if (problem) {
    setStatus(form.statusId, problem, false);
    return;
  }
  setStatus(form.statusId, "Saving…", true);
  try {
    // ops_date is the day being planned; the row's own `date` is data
    // (MM/DD on a remove_flight row), so the two travel separately.
    const resp = await post(`/api/staged_form/${form.op}`,
      { ops_date: dateIso, ...payload });
    const applied = (resp.applied || {})[form.op] || 0;
    setStatus(
      form.statusId,
      `Saved + applied (${form.op}: ${applied} row(s) materialised).`,
      true,
    );
    formEl.reset();
    // The submission wrote an override row, and the drawer is open —
    // reload the table so the new row is visible straight away.
    await Promise.all([rows.load(), deps.refreshDashboard()]);
  } catch (e) {
    setStatus(form.statusId, e.message || "Save failed.", false);
  }
}

export function init(injected) {
  deps = injected;
  for (const form of FORMS) {
    const el = $(`#${form.id}`);
    if (!el) continue;
    el.addEventListener("submit", (ev) => {
      ev.preventDefault();
      submit(form, el);
    });
  }
}
