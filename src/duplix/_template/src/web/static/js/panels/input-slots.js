/* One upload slot, rendered wherever a file belongs.
 *
 * The three files don't change on the same clock, so they don't live in
 * the same place: the flight schedule is a fresh export every morning
 * and sits on the dashboard, while the two rosters are published for a
 * whole period and sit in the Setup sidebar. Both are the same widget
 * over the same endpoints, so the slot markup, the upload and the
 * remove live here once.
 *
 * Mounts register a selector plus the kinds they own. A single load()
 * re-renders every mount from one fetch, so uploading a roster in the
 * sidebar also re-arms Plan / Allocate in the topbar — there is no
 * second source of truth about what's loaded.
 */

import { $, escapeHTML } from "../core/dom.js";
import { del, upload } from "../core/api.js";
import * as store from "../core/store.js";

/** @type {{selector: string, kinds: string[], onRender?: function}[]} */
const mounts = [];

/** Set by the owning panel's init() so this module doesn't import the
 *  run panel (which imports the views, which import this). */
let setStatus = () => {};

export function setStatusSink(fn) {
  if (typeof fn === "function") setStatus = fn;
}

/**
 * Declare a place slots get rendered into.
 * @param {string} selector  container whose innerHTML is the slot list
 * @param {string[]} kinds   input kinds this container owns, in order
 * @param {function} [onRender] called with (items, payload) after paint
 */
export function registerMount(selector, kinds, onRender) {
  mounts.push({ selector, kinds, onRender });
}

/** HH:MM out of an ISO timestamp — the date is shown separately. */
function uploadedAt(iso) {
  if (!iso) return "";
  return (iso.split("T")[1] || "").slice(0, 5);
}

function slotHtml(item) {
  const loaded = !!item.filename;
  const kind = escapeHTML(item.kind);
  const detail = loaded
    ? `${escapeHTML(item.filename)} <span class="subdued">· ${item.size_kb} KB · ${escapeHTML(uploadedAt(item.uploaded_at))}</span>`
    : `<span class="subdued">no file yet</span>`;
  const clearBtn = loaded
    ? `<button class="ghost btn-clear-input" type="button" data-kind="${kind}">Remove</button>`
    : "";
  return `
    <div class="input-slot ${loaded ? "loaded" : "empty"}" data-kind="${kind}">
      <div class="input-slot-main">
        <div class="input-slot-label">${escapeHTML(item.label)}</div>
        <div class="input-slot-file">${detail}</div>
      </div>
      <div class="input-slot-actions">
        <label class="file-btn">
          ${loaded ? "Replace" : "Choose file"}
          <input type="file" accept=".xlsx" data-kind="${kind}" hidden>
        </label>
        ${clearBtn}
      </div>
    </div>`;
}

/** The slot objects a mount owns, in the order the mount asked for. */
function itemsFor(kinds, items) {
  return kinds
    .map((k) => items.find((i) => i.kind === k))
    .filter(Boolean);
}

/** Repaint every mount from the held payload. */
export function render() {
  const payload = store.inputs.data;
  const items = (payload && payload.inputs) || [];

  for (const mount of mounts) {
    const host = $(mount.selector);
    if (!host) continue;
    const mine = itemsFor(mount.kinds, items);
    host.innerHTML = mine.map(slotHtml).join("");
    if (mount.onRender) mount.onRender(mine, payload);
  }

  // Plan and Allocate need all three, whichever place they came from.
  const ready = !!(payload && payload.ready);
  $("#run-btn")?.classList.toggle("needs-inputs", !ready);
  $("#plan-btn")?.classList.toggle("needs-inputs", !ready);
}

export async function load() {
  await store.inputs.load();
  render();
}

/** Readiness for one cadence group, straight off the last payload. */
export function groupReady(name) {
  const groups = store.inputs.data && store.inputs.data.groups;
  return !!(groups && groups[name] && groups[name].ready);
}

/** How many files that group is still waiting for. */
export function groupMissingCount(name) {
  const groups = store.inputs.data && store.inputs.data.groups;
  const missing = groups && groups[name] && groups[name].missing;
  return Array.isArray(missing) ? missing.length : 0;
}

export async function uploadFile(kind, file) {
  const slot = $(`.input-slot[data-kind="${kind}"]`);
  if (slot) slot.classList.add("uploading");
  try {
    await upload(`/api/inputs/${kind}`, file);
    await load();
    setStatus(`Loaded ${file.name}`, "ok");
  } catch (e) {
    alert(`Upload failed: ${e.message}`);
    if (slot) slot.classList.remove("uploading");
  }
}

async function clearOne(kind) {
  try {
    await del(`/api/inputs/${kind}`);
  } catch (e) {
    alert(`Remove failed: ${e.message}`);
    return;
  }
  await load();
}

/** Wire one container's slots. Delegated, so a repaint keeps working. */
export function bindMount(selector) {
  const host = $(selector);
  if (!host) return;

  host.addEventListener("change", (ev) => {
    const input = ev.target;
    if (input.tagName !== "INPUT" || input.type !== "file") return;
    const file = input.files && input.files[0];
    if (file) uploadFile(input.dataset.kind, file);
    input.value = "";      // allow re-picking the same filename
  });

  host.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".btn-clear-input");
    if (btn) clearOne(btn.dataset.kind);
  });
}
