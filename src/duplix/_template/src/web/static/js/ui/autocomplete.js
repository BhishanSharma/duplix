/* A small "type and see suggestions" dropdown for a text input — the
 * same shape as a search-engine's suggestion list: matches update as
 * you type, "starts with" ranks above "contains", arrow keys move the
 * highlight, Enter or a click accepts it.
 *
 * It only fills the input and re-fires its `input` event — it never
 * performs the panel's own action (adding a ZC, applying a shift…).
 * That stays a deliberate second step (click the row / hit Apply),
 * same as before; this just makes the name faster to land on.
 */

import { escapeHTML } from "../core/dom.js";

const MAX_RESULTS = 8;

/** Rank a pool of items by how `query` matches their name: "starts
 *  with" first, then "contains", each in original order, de-duplicated
 *  by name. `nameOf` pulls the display name off a pool item (defaults
 *  to `.name`, or the item itself if it's already a string). */
export function rank(pool, query, nameOf = (x) => String(x?.name ?? x ?? "")) {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const starts = [];
  const contains = [];
  const seen = new Set();
  for (const item of pool) {
    const name = nameOf(item);
    const norm = name.toLowerCase();
    if (!norm.includes(q) || seen.has(norm)) continue;
    seen.add(norm);
    (norm.startsWith(q) ? starts : contains).push(item);
  }
  return [...starts, ...contains];
}

function highlight(text, query) {
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return escapeHTML(text);
  const before = text.slice(0, idx);
  const match = text.slice(idx, idx + query.length);
  const after = text.slice(idx + query.length);
  return `${escapeHTML(before)}<strong>${escapeHTML(match)}</strong>${escapeHTML(after)}`;
}

function ensureWrap(input) {
  const parent = input.parentElement;
  if (parent && parent.classList.contains("autocomplete-wrap")) return parent;
  const wrap = document.createElement("div");
  wrap.className = "autocomplete-wrap";
  parent.insertBefore(wrap, input);
  wrap.appendChild(input);
  return wrap;
}

/** Attach suggestions to `input`.
 *  `getCandidates(query)` returns `{ label, sublabel? }[]` — already
 *  ranked, this only takes the top few and draws them. */
export function attach(input, { getCandidates, maxResults = MAX_RESULTS }) {
  if (!input) return;
  const wrap = ensureWrap(input);
  const list = document.createElement("ul");
  list.className = "autocomplete-list";
  list.hidden = true;
  wrap.appendChild(list);

  let items = [];
  let activeIndex = -1;

  function close() {
    list.hidden = true;
    list.innerHTML = "";
    items = [];
    activeIndex = -1;
  }

  function draw(query) {
    if (!items.length) { close(); return; }
    list.innerHTML = items.map((item, i) => `
      <li class="autocomplete-item${i === activeIndex ? " active" : ""}" data-index="${i}">
        <span>${highlight(item.label, query)}</span>
        ${item.sublabel ? `<small>${escapeHTML(item.sublabel)}</small>` : ""}
      </li>`).join("");
    list.hidden = false;
  }

  function open(query) {
    items = (getCandidates(query) || []).slice(0, maxResults);
    activeIndex = -1;
    draw(query);
  }

  function select(item) {
    input.value = item.label;
    close();
    // Re-fire input so the panel's own listener re-filters its list
    // below, exactly as if the operator had typed the full name.
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  }

  input.addEventListener("input", () => {
    const query = input.value.trim();
    if (!query) { close(); return; }
    open(query);
  });

  input.addEventListener("focus", () => {
    const query = input.value.trim();
    if (query) open(query);
  });

  input.addEventListener("keydown", (ev) => {
    if (list.hidden) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      activeIndex = Math.min(activeIndex + 1, items.length - 1);
      draw(input.value.trim());
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      draw(input.value.trim());
    } else if (ev.key === "Enter") {
      if (activeIndex >= 0 && items[activeIndex]) {
        ev.preventDefault();
        select(items[activeIndex]);
      } else {
        close();
      }
    } else if (ev.key === "Escape") {
      close();
    }
  });

  // mousedown (not click) fires before the input's blur closes the
  // list, so a click on a suggestion is never swallowed by the blur.
  list.addEventListener("mousedown", (ev) => {
    const li = ev.target.closest(".autocomplete-item");
    if (!li) return;
    ev.preventDefault();
    const item = items[Number(li.dataset.index)];
    if (item) select(item);
  });

  input.addEventListener("blur", () => setTimeout(close, 100));
}
