/* The Plan readback.
 *
 * Its visible output is small — it decides whether the post-Plan UI
 * (the Override button, the handler panel) is available, and flags the
 * one failure mode worth calling out: a Plan that ran but extracted
 * zero flights, which is almost always the wrong file in the flight
 * schedule slot.
 *
 * The per-shift coverage bars this used to draw were removed once the
 * headcount moved under each workload distribution bar; the block
 * survives as a hidden div so the visibility flag has somewhere to go.
 */

import { $ } from "../core/dom.js";
import * as store from "../core/store.js";
import * as inputs from "./inputs.js";

export const template = `<div id="plan-block" hidden></div>`;

/** Show or hide everything that only makes sense once Plan has run.
 *  Once the Override button is shown it stays shown — hiding it on a
 *  later refresh produced a flicker between Plan completing and the
 *  next poll. */
export function setPostPlanUI(planHasRun) {
  if (planHasRun) {
    $("#open-override").hidden = false;
  } else {
    $("#open-override").hidden = true;
    $("#handlers-block").hidden = true;
  }
}

export async function load() {
  await store.plan.load();
  const p = store.plan.data;
  if (!p) return;

  const totalFlights = p.total_flights ?? 0;
  const coverage = p.coverage_by_shift || {};
  const anyStaff = Object.values(coverage).some((c) => (c.present ?? 0) > 0);
  const planHasRun = totalFlights > 0 || anyStaff;

  // Visibility first, before any rendering work: if a later step throws,
  // the dashboard still reflects the correct shown/hidden state.
  $("#plan-block").hidden = !planHasRun;
  setPostPlanUI(planHasRun);

  if (Boolean(p.text) && totalFlights === 0) {
    inputs.warn(
      "Plan found no flights — check the flight schedule file is "
      + "today's SV portal export.",
    );
  }
}
