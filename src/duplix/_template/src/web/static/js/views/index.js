/* The tab registry.
 *
 * Order here is the order of the tab strip. Each view exports:
 *   id        the tab / panel id
 *   label     the tab button text
 *   pillId    optional — id of a count badge in the tab button
 *   template  the panel's markup
 *   init(deps) wire listeners; runs once, after the template is mounted
 *   load()    fetch and render
 *
 * Adding a tab: write the module, import it, add it to VIEWS.
 */

import * as dashboard from "./dashboard.js";
import * as allocations from "./allocations.js";
import * as unallocated from "./unallocated.js";
import * as workload from "./workload.js";
import * as pairs from "./pairs.js";
import * as warnings from "./warnings.js";

export const VIEWS = [
  dashboard,
  allocations,
  unallocated,
  workload,
  pairs,
  warnings,
];

/** Views that don't need the shared deps still get called with them, so
 *  every view's init has the same signature. */
export const VIEW_IDS = VIEWS.map((v) => v.id);
