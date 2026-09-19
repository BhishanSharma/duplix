/* The operating-date input in the topbar.
 *
 * Changing the date re-reads everything for that date, so a prior
 * allocation loads without a manual refresh. It does not refetch the
 * inputs — those are date-independent — and the next Plan / Allocate
 * stamps whatever date is showing.
 *
 * The change is hooked on three events because the operator can commit
 * a date three ways: picking from the calendar, typing and pressing
 * Enter, or typing and clicking away. Reset doesn't re-emit 'change',
 * so without the Enter and blur hooks they'd have to click out and
 * back in to see anything happen.
 *
 * The starting value comes from the server, never from the browser —
 * see adoptServerDate().
 */

import { $ } from "../core/dom.js";
import { getOr } from "../core/api.js";
import * as tabs from "../ui/tabs.js";
import * as store from "../core/store.js";
import * as plan from "./plan.js";
import * as handlers from "./handlers.js";
import * as staffing from "./staffing.js";
import * as flightTrend from "../charts/flight-trend.js";

let lastApplied = "";

/** Forget the last-applied date, so re-committing the same one still
 *  refreshes. Reset needs this: it wipes the results for the current
 *  date, and without forgetting, the dashboard would keep showing the
 *  pre-Reset state until a *different* date was typed. */
export function forget() {
  lastApplied = "";
}

export function value() {
  return $("#run-date").value;
}

/** Fill the input from the server's clock on first paint.
 *
 * The input used to start empty, so every morning began with picking
 * today out of a date picker — and the date that mattered was the
 * server's, not the browser's. ``/api/server_date`` answers with the
 * session's run date when there is one (a reload mid-day keeps the day
 * being worked on) and the server's today when there isn't.
 *
 * Operator-editable on purpose: back-filling yesterday is a real task.
 * A date the operator has already typed is never overwritten.
 */
export async function adoptServerDate() {
  const input = $("#run-date");
  if (!input || input.value) return input ? input.value : "";
  const payload = await getOr("/api/server_date", null, "serverDate");
  const d = payload && (payload.effective || payload.run_date || payload.date);
  if (!d) return "";
  input.value = d;
  // Setting .value fires no 'change', and the readbacks are already in
  // flight against the same server-side date, so record it as applied
  // rather than kicking off a second round of fetches.
  lastApplied = d;
  return d;
}

async function apply({ force = false } = {}) {
  const d = value();
  if (!d) return;
  if (!force && d === lastApplied) return;
  lastApplied = d;

  store.invalidateAll();
  flightTrend.clear();

  await Promise.all([
    tabs.loadTab("dashboard"),
    plan.load(),
    handlers.load(),
    staffing.load(),
  ]);
  const active = tabs.activeTab();
  if (active !== "dashboard") await tabs.loadTab(active);
}

export function init() {
  const input = $("#run-date");
  input.addEventListener("change", () => apply());
  input.addEventListener("blur", () => apply({ force: true }));
  input.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    apply({ force: true });
  });
}
