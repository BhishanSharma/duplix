/* Shift vocabulary shared across charts, tables and the drawer.
 *
 * One place to add a shift: a new code here plus its CSS class in
 * css/features/shifts.css, and every chart, legend and dropdown picks
 * it up.
 */

/** Left-to-right order used by every per-shift view. */
export const SHIFT_ORDER = ["M", "M1", "A", "A1", "N"];

/** Order the dropdowns use (matches the roster's own listing). */
export const SHIFT_OPTIONS = ["M", "A", "N", "M1", "A1"];

/** Mirrored from the .staff-shift-* CSS classes so SVG fills match the
 *  chips. M1 / A1 are darker so they read as distinct from M / A. */
export const SHIFT_COLOR = {
  M: "#cfe7ff", M1: "#6fa8dc", A: "#ffe2b8", A1: "#e8843c", N: "#d6c7ec",
};

/** Per-shift workload ramp, light -> dark for low -> high load. Hand
 *  tuned: an opacity-only ramp looked monotonous. */
export const SHIFT_LEVEL_PALETTE = {
  M:  ["#deeeff", "#b3d4f5", "#7fb5e8", "#4f8ed4"],   // sky → cobalt
  M1: ["#cee0ee", "#9bbedb", "#6b9fc5", "#3a78a8"],   // pale → deep blue
  A:  ["#fff0d6", "#ffd49a", "#ffae5d", "#e88b2c"],   // cream → tangerine
  A1: ["#fbdcc1", "#f0b988", "#dd8e4a", "#b96b22"],   // sand → burnt
  N:  ["#ece2f5", "#cab3e0", "#a48dca", "#7d62b0"],   // wisteria → plum
};

export const FALLBACK_LEVEL_PALETTE = ["#e0e0e0", "#bdbdbd", "#9e9e9e", "#616161"];

/** Shift nominal windows, in ops-day minutes from 00:00 D-day (N wraps
 *  into D+1). Mirror of allocator/windows.py:SHIFT_NOMINAL_MIN — the
 *  trend chart paints its bands and per-hour stats from these. */
export const SHIFT_WINDOWS_MIN = {
  M:  [4 * 60,         12 * 60 + 30],
  M1: [6 * 60,         14 * 60 + 30],
  A:  [12 * 60 + 30,   21 * 60],
  A1: [14 * 60 + 30,   23 * 60],
  N:  [20 * 60 + 30,   24 * 60 + 5 * 60],   // 20:30 D -> 05:00 D+1
};

/** The operational day the trend chart covers: 25 hourly buckets from
 *  05:00 D through 05:00 D+1, the last one holding the preplan tail. */
export const TREND_BUCKETS = 25;

/** Roles the drawer's change_role rows can target. */
export const ROLE_OPTIONS = ["STAFF", "ZC", "AM"];
