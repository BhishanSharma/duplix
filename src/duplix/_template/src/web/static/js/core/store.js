/* Every server payload the UI holds on to, in one registry.
 *
 * Views need the last payload around after the fetch: changing a filter
 * re-renders from memory rather than re-hitting the server. That used
 * to be a module-level `_fooCache` per view, and every reset path — new
 * run, new date, Reset button — had to remember to null each one by
 * hand. Forgetting one showed the operator stale numbers.
 *
 * Here a resource declares itself once and `invalidateAll()` clears the
 * lot, so adding a resource can't introduce a stale-cache bug.
 */

import { getOr } from "./api.js";

const registry = new Map();

/**
 * @param {object} spec
 * @param {string} spec.key      registry name, used by invalidate()
 * @param {string} spec.path     API path to GET
 * @param {*}      spec.fallback value served when the fetch fails
 * @param {boolean} [spec.cache] true = reuse the held payload instead of
 *                               re-fetching, until something clears it
 * @param {function} [spec.select] maps the payload to what callers want
 */
function createResource({ key, path, fallback, cache = false, select }) {
  let held;                       // undefined = never loaded
  const resource = {
    key,
    /** The last loaded value, for a synchronous re-render. */
    get data() {
      return held === undefined ? fallback : held;
    },
    /** True once a load has completed (even a failed one). */
    get loaded() {
      return held !== undefined;
    },
    async load({ force = false } = {}) {
      if (cache && !force && held !== undefined) return held;
      const payload = await getOr(path, fallback, key);
      held = select ? select(payload) : payload;
      return held;
    },
    clear() {
      held = undefined;
    },
  };
  registry.set(key, resource);
  return resource;
}

const rows = (payload) => (payload && payload.rows) || [];

export const dashboard = createResource({
  key: "dashboard", path: "/api/dashboard", fallback: {},
});

export const allocations = createResource({
  key: "allocations", path: "/api/allocations", fallback: [],
  cache: true, select: rows,
});

export const workload = createResource({
  key: "workload", path: "/api/workload", fallback: [], select: rows,
});

export const pairs = createResource({
  key: "pairs", path: "/api/pairs", fallback: [], cache: true, select: rows,
});

export const warnings = createResource({
  key: "warnings", path: "/api/warnings", fallback: [], select: rows,
});

export const unallocated = createResource({
  key: "unallocated", path: "/api/unallocated", fallback: { count: 0, rows: [] },
});

export const recommendations = createResource({
  key: "recommendations", path: "/api/recommendations",
  fallback: { recommendations: [] },
});

export const staffNames = createResource({
  key: "staffNames", path: "/api/staff_names", fallback: [],
  select: (p) => (p && p.names) || [],
});

export const handlers = createResource({
  key: "handlers", path: "/api/handlers", fallback: null,
});

export const staffing = createResource({
  key: "staffing", path: "/api/staffing", fallback: null,
});

export const plan = createResource({
  key: "plan", path: "/api/plan", fallback: null,
});

export const inputs = createResource({
  key: "inputs", path: "/api/inputs", fallback: null,
});

export const overrides = createResource({
  key: "overrides", path: "/api/overrides", fallback: { headers: [], rows: [] },
});

export const overrideTypes = createResource({
  key: "overrideTypes", path: "/api/override_types", fallback: null,
  cache: true,
});

export const shiftLimits = createResource({
  key: "shiftLimits", path: "/api/shift_limits",
  fallback: { bands: {}, iteration_overrides: {} },
});

/** name -> shift, derived from the roster. Charts and the allocations
 *  table colour every name by the named person's own shift, so they all
 *  read from this one index rather than each building their own. */
export function shiftByStaff() {
  const index = {};
  for (const r of staffNames.data) {
    if (r.name && r.shift) index[r.name.trim()] = r.shift.trim();
  }
  return index;
}

/** Drop the held payload for the named resources. */
export function invalidate(...keys) {
  for (const key of keys) registry.get(key)?.clear();
}

/** Drop everything. Called after a run, a Reset, and a date change —
 *  the three moments when any held payload could be stale. */
export function invalidateAll() {
  for (const resource of registry.values()) resource.clear();
}

/** Registered resource keys — useful when debugging from the console. */
export const keys = () => Array.from(registry.keys());
