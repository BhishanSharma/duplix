"""Step 1 — clean SV portal flights (plan §10 Phase 1).

Reads the uploaded flight schedule and groups the surviving rows by
OpsClass into ``state.cleaned``.

Drop filter (Phase 3 INTL overhaul — 2026-05-14, amended 2026-05-19):
  1. drop rows where date is None or dep_time is None
  2. drop rows where date not in {D, D+1}
  3. drop rows where date == D and std < 05:05  (asymmetric — see plan §1.5)
  4. drop rows where date == D+1 and std > 05:30 (Change 7: widened from
     05:05 to 05:30 — flights at 05:05-05:30 D+1 are KEPT with the
     ``is_preplan_deferred=True`` flag, re-appended by Step 3 as
     ``warning="PREPLAN_DEFERRED"``)
  5. drop rows where dep is None or arr is None
  6. drop rows where ops_class cannot be derived
  7. drop rows where the cleaned-row schema rejects the values

2026-05-19 amendment (Gulf routing):
  * Gulf-3 DEP flights (AUH / DOH / DXB) and QR-owner flights are no
    longer DROPPED. They route to OpsClass.GULF and land on the
    dedicated GULF class for reference. Step 3 does not
    read that sheet, so these flights are never allocated to staff —
    same operational outcome as the legacy drop, but with audit-trail
    visibility.
  * ``routing_drop_dep_codes`` is now defunct; the new
    ``ops_class_gulf_dep_codes`` + ``ops_class_gulf_owner_codes``
    drive the routing.

Phase 3 amendments (2026-05-14):
  * Change 9: ``is_international = (dep ∈ intl_codes)`` — DEP-side only
    (was DEP or ARR). Pink-highlight machinery removed.
  * Change 7: D+1 STD 05:05-05:30 enters pool with ``is_preplan_deferred``.

Pax (Booked Pax) is read for reference only as of 2026-05-10 — it does
NOT drop rows or influence allocation. Unparseable pax strings (CV,
empty, etc.) are kept with load=0.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date as date_type
from datetime import time, timedelta
from pathlib import Path

from .config import Config, load_config
from .io.readers import read_sv_portal, workbook_from_bytes
from .schemas import CleanFlightRow, OpsClass, RawFlightRow
from .state import AppState

CUTOFF: time = time(5, 5)
# Phase 3 / Change 7: D+1 flights with STD in [CUTOFF, DEFERRED_UPPER]
# are KEPT (flagged is_preplan_deferred); D+1 > DEFERRED_UPPER is dropped.
DEFERRED_UPPER: time = time(5, 30)


def _safe_load(pax_raw: str | None) -> int:
    """Best-effort parse of a Booked Pax string to an int load. Pax is
    reference-only since 2026-05-10 — unparseable values default to 0
    rather than dropping the flight."""
    if not pax_raw or not isinstance(pax_raw, str):
        return 0
    tail = pax_raw.rsplit("/", 1)[-1].strip()
    try:
        n = int(tail)
    except (ValueError, TypeError):
        return 0
    return max(0, min(600, n))


def _derive_ops_class(
    aircraft_type: str | None,
    owner: str | None,
    dep: str,
    arr: str,
    row_date: date_type,
    d_day: date_type,
    ops_map: Mapping[str, str],
    gulf_dep_codes: frozenset[str],
    gulf_owner_codes: frozenset[str],
) -> OpsClass | None:
    """Routing precedence (highest to lowest):

    1. GULF — DEP in ``gulf_dep_codes`` (AUH / DOH / DXB) OR owner /
       TYPE letter in ``gulf_owner_codes`` (e.g. ``QR``). Extract-only:
       the cleaned row lands in the GULF class but Step 3 ignores
       that sheet, so these flights never enter the solver. Per user
       direction 2026-05-19.
    2. TYPE = X → TEST or FERRY based on routing (user direction
       2026-05-11):
         * dep == arr (same-airport loop) → TEST
         * dep != arr (positioning leg)   → FERRY
    3. Flat TYPE-letter mapping — H → P2F, B → CHARTER, K/P/T/G/W → FERRY.
    4. Date-based fallback — D → DAY, D+1 → NIGHT (J / unknown types
       routing to passenger ops).
    """
    owner_upper = (owner or "").strip().upper()
    t_upper = (aircraft_type or "").strip().upper()
    # 2026-05-19: GULF detection wins outright. DEP-side OR owner-side
    # match routes the flight to the extract-only GULF class.
    if dep in gulf_dep_codes:
        return OpsClass.GULF
    if owner_upper and owner_upper in gulf_owner_codes:
        return OpsClass.GULF
    if t_upper and t_upper in gulf_owner_codes:
        return OpsClass.GULF
    if t_upper == "X":
        return OpsClass.TEST if dep == arr else OpsClass.FERRY
    if t_upper:
        mapped = ops_map.get(t_upper)
        if mapped is not None:
            try:
                return OpsClass(mapped)
            except ValueError:
                return None
    if row_date == d_day:
        return OpsClass.DAY
    if row_date == d_day + timedelta(days=1):
        return OpsClass.NIGHT
    return None


def _matches_filter(
    f: "dict[str, str]",
    *, ac_type: str, ac_owner: str, ac: str, dep: str, arr: str,
    extras: "dict[str, str]",
) -> bool:
    """Return True iff every non-empty field in ``f`` matches the
    corresponding flight value (case-insensitive exact). Empty/missing
    fields are skipped — they don't constrain the match. A filter
    with every field blank never matches anything (returns False) so
    a blank filter row in config can't accidentally drop the whole
    pool."""
    def _eq(target: str | None, actual: str) -> bool:
        return bool(target and target.strip()
                    and target.strip().upper() == actual.strip().upper())
    constraints = [
        ("ac_type", ac_type),
        ("ac_owner", ac_owner),
        ("ac", ac),
        ("dep", dep),
        ("arr", arr),
    ]
    active = False
    for key, actual in constraints:
        want = f.get(key) or ""
        if want.strip():
            active = True
            if not _eq(want, actual):
                return False
    # Custom-header constraint.
    ch = (f.get("custom_header") or "").strip()
    cv = (f.get("custom_value") or "").strip()
    if ch and cv:
        active = True
        cell = extras.get(ch) or ""
        if cell.strip().upper() != cv.upper():
            return False
    return active


def clean_flights(
    raw_rows: Iterable[RawFlightRow],
    d_day: date_type,
    config: Config,
) -> dict[OpsClass, list[CleanFlightRow]]:
    d_plus_1 = d_day + timedelta(days=1)
    # Phase 3 / Change 9: DEP-side only INTL definition.
    intl_codes = {c.upper() for c in config.io.sv_portal.international_airport_codes}
    ops_map = {k.upper(): v for k, v in config.ops_class_by_aircraft_type.items()}
    # 2026-05-19: GULF routing — extract-only sheet, never allocated.
    gulf_dep_codes = frozenset(
        c.strip().upper() for c in config.ops_class_gulf_dep_codes if c.strip()
    )
    gulf_owner_codes = frozenset(
        c.strip().upper() for c in config.ops_class_gulf_owner_codes if c.strip()
    )
    # 2026-05-25: user-defined extraction filters. Flights matching any
    # filter route to OpsClass.GULF — same destination as Gulf-DEP
    # flights (extract-only, never allocated).
    extraction_filters = list(config.extraction_filters or [])

    out: dict[OpsClass, list[CleanFlightRow]] = defaultdict(list)

    for r in raw_rows:
        if r.date is None or r.dep_time is None:
            continue
        if r.date not in (d_day, d_plus_1):
            continue
        if r.date == d_day and r.dep_time < CUTOFF:
            continue
        # Phase 3 / Change 7: D+1 keep-window widened to [CUTOFF, DEFERRED_UPPER]
        # inclusive. STD in that band is flagged is_preplan_deferred. Anything
        # past DEFERRED_UPPER on D+1 is dropped.
        if r.date == d_plus_1 and r.dep_time > DEFERRED_UPPER:
            continue
        if r.departure is None or r.arrival is None:
            continue
        dep = r.departure[:3].upper()
        arr = r.arrival[:3].upper()
        # 2026-05-19: Gulf-3 + QR-owner flights are no longer dropped.
        # They route to OpsClass.GULF (extract-only) — see
        # _derive_ops_class precedence. Step 3 ignores the GULF class
        # so they stay out of the solver naturally.
        # Pax is reference-only (user direction 2026-05-10) — never
        # dropped on this basis. Unparseable pax becomes load=0.
        load = _safe_load(r.booked_pax_raw)
        ops = _derive_ops_class(
            r.aircraft_type, r.owner,
            dep, arr, r.date, d_day, ops_map,
            gulf_dep_codes, gulf_owner_codes,
        )
        if ops is None:
            continue
        # 2026-05-25: user-defined extraction filters override the
        # derived ops_class. If any filter matches, route the flight
        # to OpsClass.GULF (extract-only sheet) regardless of what
        # _derive_ops_class returned. Filters run AFTER ops_class
        # derivation so the original class is known (helpful for
        # debugging / log output later if needed) but the destination
        # is overridden to GULF when matched.
        if extraction_filters:
            ac_type_val = str(r.aircraft_type or "")
            ac_owner_val = str(r.owner or "")
            ac_val = str(r.aircraft_subtype or "")
            extras = r.extra_columns or {}
            for f in extraction_filters:
                if _matches_filter(
                    f,
                    ac_type=ac_type_val, ac_owner=ac_owner_val,
                    ac=ac_val, dep=dep, arr=arr, extras=extras,
                ):
                    ops = OpsClass.GULF
                    break
        # Change 7: deferred = D+1 row whose STD is at/after CUTOFF
        # (D+1 < CUTOFF is the existing post-midnight NIGHT tail).
        is_deferred = (r.date == d_plus_1 and r.dep_time >= CUTOFF)
        try:
            cleaned = CleanFlightRow(
                date=r.date,
                flt=str(r.flight_id) if r.flight_id is not None else "",
                type=str(r.aircraft_type or ""),
                ac=str(r.aircraft_subtype or ""),
                dep=dep,
                arr=arr,
                std=r.dep_time,
                load=load,
                ops_class=ops,
                # Change 9 (2026-05-14): DEP-side only.
                # 2026-05-16: P2F flights are NEVER marked international
                # even when their DEP airport (e.g. DAC, KMG) is in the
                # INTL list. P2F has its own dedicated routing + color
                # convention; the INTL flag is for regular DEP-side INTL
                # passenger ops only.
                is_international=(dep in intl_codes
                                  and ops != OpsClass.P2F),
                is_preplan_deferred=is_deferred,
            )
        except ValueError:
            continue
        out[ops].append(cleaned)

    return dict(out)


def run(
    state: AppState,
    d_day: date_type,
    config_path: Path | str = "configs/config.yml",
) -> dict[OpsClass, int]:
    """Clean the uploaded flight schedule into ``state.cleaned``.

    Returns the row count per OpsClass. Raises FileNotFoundError when
    the schedule has not been uploaded yet.
    """
    config = load_config(config_path)
    wb = workbook_from_bytes(state.input_bytes("sv_portal"))
    try:
        raw = read_sv_portal(wb, config)
    finally:
        wb.close()
    by_class = clean_flights(raw, d_day, config)
    with state.lock:
        state.cleaned = by_class
    return {k: len(v) for k, v in by_class.items()}
