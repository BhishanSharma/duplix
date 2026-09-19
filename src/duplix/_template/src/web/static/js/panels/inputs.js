/* Daily Flight SV schedule upload from the top navigation. */

import { $ } from "../core/dom.js";
import * as slots from "./input-slots.js";

export const template = "";
export const load = slots.load;

let setRunStatus = () => {};

/** Report a source-file problem in the shared topbar status pill. */
export function warn(message) {
  setRunStatus(message, "warn");
}

export function init(deps) {
  setRunStatus = deps.setRunStatus;
  slots.setStatusSink(deps.setRunStatus);
  const button = $("#upload-btn");
  const input = $("#flight-upload-input");
  button.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (file) slots.uploadFile("sv_portal", file);
    input.value = "";  // allow re-selecting the same export
  });
}
