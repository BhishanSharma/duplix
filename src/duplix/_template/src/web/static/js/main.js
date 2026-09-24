/* Entry point: mount the markup, wire everything, load the first data.
 *
 * Strict order, and the reason for each step:
 *   1. mount    — every module's markup lands in the DOM
 *   2. init     — listeners attach, now that their elements exist
 *   3. boot     — the first round of fetches
 *
 * Modules never do work at import time; they export init() and load().
 * That's what keeps the order here meaningful.
 */

import { $ } from "./core/dom.js";
import * as layout from "./ui/layout.js";
import * as tabs from "./ui/tabs.js";
import * as confirmDialog from "./ui/confirm.js";
import * as setup from "./sidebar/index.js";
import * as run from "./panels/run.js";
import * as plan from "./panels/plan.js";
import * as handlers from "./panels/handlers.js";
import * as staffing from "./panels/staffing.js";
import * as dateControl from "./panels/date-control.js";

async function openSetup() {
  await setup.open();
}

/** What the cross-cutting modules need from each other, passed in
 *  rather than imported, so nothing has to reach across the tree (and
 *  no import cycles form). */
const deps = {
  currentDate: () => dateControl.value(),
  setRunStatus: run.setRunStatus,
  triggerRun: run.triggerRun,
  openSetup,
  /** Everything an override edit can change, refreshed together. */
  refreshDashboard: async () => {
    await Promise.allSettled([
      tabs.loadTab("dashboard"),
      plan.load(),
      handlers.load(),
      staffing.load(),
    ]);
  },
};

function init() {
  layout.mount();

  confirmDialog.init();
  setup.init();
  tabs.init(deps);
  run.init();
  dateControl.init();
  layout.initGlobalHandlers();

  $("#open-setup").addEventListener("click", openSetup);
  $("#reset-btn").addEventListener("click", () => {
    // Forget the applied date so re-committing the same one still
    // refreshes — otherwise the dashboard would keep showing the
    // pre-Reset state until a different date was typed.
    dateControl.forget();
    run.triggerReset();
  });
}

/** First paint. Each readback is a small payload and the server is
 *  threaded, so these go out together rather than in a chain. */
function boot() {
  // The operating date comes from the server, not the browser: this
  // console is driven from whichever machine is free, and a laptop on a
  // stale clock or another timezone would otherwise plan the wrong day.
  dateControl.adoptServerDate();
  tabs.loadTab("dashboard");
  plan.load();
  handlers.load();
  staffing.load();
  run.poll();          // pick up a run already in flight
}

init();
boot();
