/* Toast notifications — replaces window.alert().
 *
 * alert() blocks the whole tab and renders with a "127.0.0.1:8765
 * says" prefix that reads like a browser security warning —
 * confirm.js already solved this for window.confirm(). This is the
 * alert() equivalent: non-blocking, stacks top-right, colored by
 * kind, and dismisses itself (errors linger longer than a quick
 * success) unless the operator is reading it — hover pauses the
 * timer, and a close button or Escape ends it early.
 */

import { $, escapeHTML } from "../core/dom.js";

export const template = `<div id="toast-stack" aria-live="polite"></div>`;

const DURATION_MS = { error: 8000, warn: 6000, ok: 4000, info: 5000 };

const ICON = {
  error: "&#9888;",  // ⚠
  warn: "&#9888;",   // ⚠
  ok: "&#10003;",    // ✓
  info: "&#8505;",   // ℹ
};

let seq = 0;

function dismiss(el) {
  if (!el || el.dataset.leaving) return;
  el.dataset.leaving = "1";
  el.classList.add("toast-leaving");
  el.addEventListener("animationend", () => el.remove(), { once: true });
}

/** Show a toast. kind: "error" | "warn" | "ok" | "info" (default "info").
 *  Returns the element, mainly so callers/tests can dismiss it early. */
export function show(message, kind = "info") {
  const stack = $("#toast-stack");
  if (!stack) return null;

  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.id = `toast-${++seq}`;
  el.innerHTML = `
    <span class="toast-icon" aria-hidden="true">${ICON[kind] || ICON.info}</span>
    <span class="toast-message">${escapeHTML(message)}</span>
    <button class="toast-close" type="button" aria-label="Dismiss">&times;</button>`;
  stack.appendChild(el);

  let timer = null;
  const duration = DURATION_MS[kind] ?? DURATION_MS.info;
  const start = () => { timer = setTimeout(() => dismiss(el), duration); };
  const stop = () => { if (timer) clearTimeout(timer); timer = null; };

  el.addEventListener("mouseenter", stop);
  el.addEventListener("mouseleave", start);
  el.querySelector(".toast-close").addEventListener("click", () => {
    stop();
    dismiss(el);
  });

  start();
  return el;
}

export const error = (message) => show(message, "error");
export const warn = (message) => show(message, "warn");
export const ok = (message) => show(message, "ok");
export const info = (message) => show(message, "info");

/** Dismiss the most recently shown toast still on screen. No-ops if
 *  none are open. Wired to Escape in layout.js. */
export function dismissTop() {
  const stack = $("#toast-stack");
  const last = stack?.lastElementChild;
  if (last) dismiss(last);
}

export function isOpen() {
  const stack = $("#toast-stack");
  return !!stack && stack.childElementCount > 0;
}
