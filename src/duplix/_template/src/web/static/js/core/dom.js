/* DOM helpers. The whole front end goes through these three. */

export const $ = (sel, root) => (root || document).querySelector(sel);

export const $$ = (sel, root) =>
  Array.from((root || document).querySelectorAll(sel));

/** Escape a value for interpolation into an HTML template literal.
 *  Every piece of server data goes through this — staff names and
 *  airport codes are operator-entered and land straight in innerHTML. */
export const escapeHTML = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

/** Replace a host element's contents with rendered HTML.
 *  Returns the host so callers can chain a querySelector for wiring. */
export function render(host, html) {
  if (!host) return null;
  host.innerHTML = html;
  return host;
}

/** Set textContent without caring whether the element exists. Half the
 *  dashboard is optional blocks, so a null-safe setter keeps the render
 *  paths free of `if (el)` noise. */
export function setText(sel, value, fallback = "—") {
  const el = typeof sel === "string" ? $(sel) : sel;
  if (el) el.textContent = value == null || value === "" ? fallback : String(value);
}

/** Show/hide via the `hidden` attribute (the CSS gives it precedence). */
export function setHidden(sel, hidden) {
  const el = typeof sel === "string" ? $(sel) : sel;
  if (el) el.hidden = !!hidden;
}
