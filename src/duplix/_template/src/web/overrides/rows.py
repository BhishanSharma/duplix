"""Override-row mutation: the drawer's table rows, the recommender's
one-click fixes, and the staged add/remove forms.

Override rows live in ``state.STATE.overrides`` as dicts keyed by the
column names in ``state.OVERRIDE_HEADERS``. Each row type uses its own
subset of those keys (a ``p2f`` row cares about employee + shift; a
``waive_h10_pair`` row about flight + std + other_std), so nothing here
validates the shape — the reader for each type does that at solve time,
exactly as it would for a row typed in by hand.
"""

from __future__ import annotations

from ...state import OVERRIDE_HEADERS, OVERRIDE_HEADERS_VISIBLE, AppState

# Which columns each recommender "kind" must supply.
_PHASE_R_PAYLOAD_COLS = {
    "waive_h10_pair":    ("flight", "std", "employee", "other_std"),
    "raise_cap":         ("flight", "std", "employee"),
    "skip_p2f_buffer":   ("flight", "std", "employee"),
    "skip_intl_removal": ("flight", "std"),
}


def add_override(state: AppState, values: list[str]) -> int:
    """Append a drawer row. ``values`` are positional against
    ``OVERRIDE_HEADERS_VISIBLE``. Returns the new row's index."""
    row = {
        h: v for h, v in zip(OVERRIDE_HEADERS_VISIBLE, values)
        if str(v).strip()
    }
    return state.add_override(row)


def update_override(state: AppState, index: int, values: list[str]) -> None:
    """Overwrite the visible cells of row ``index``, preserving any
    type-specific fields (Phase-R flight / std) already on the row."""
    with state.lock:
        if not 0 <= index < len(state.overrides):
            raise IndexError(f"no override row at index {index}")
        merged = dict(state.overrides[index])
        for h, v in zip(OVERRIDE_HEADERS_VISIBLE, values):
            s = str(v).strip()
            if s:
                merged[h] = s
            else:
                merged.pop(h, None)
        state.update_override(index, merged)


def delete_override(state: AppState, index: int) -> None:
    state.delete_override(index)


def clear_all_overrides(state: AppState) -> int:
    """Drop every override row. Returns the count removed.

    A fresh Plan resets the list so leftover nominations from the
    previous cycle don't silently shadow the new roster's handler picks.
    """
    with state.lock:
        n = len(state.overrides)
        state.overrides = []
        return n


def apply_phase_r_override(
    state: AppState, kind: str, payload: dict[str, str],
) -> int:
    """Write a Phase-R recommender suggestion as an override row.

    ``kind`` is one of waive_h10_pair / raise_cap / skip_p2f_buffer /
    skip_intl_removal. Returns the new row's index. Raises ValueError on
    an unknown kind and KeyError when a required field is missing.
    """
    if kind not in _PHASE_R_PAYLOAD_COLS:
        raise ValueError(f"unknown recommender kind: {kind!r}")
    row: dict[str, str] = {"type": kind}
    for col in _PHASE_R_PAYLOAD_COLS[kind]:
        value = str(payload.get(col, "")).strip()
        if not value:
            raise KeyError(f"missing payload field {col!r} for kind {kind!r}")
        row[col] = value
    return state.add_override(row)


def write_staged_override(
    state: AppState, op_type: str, payload: dict[str, str],
) -> int:
    """Write a staged-form row (Add flight / Remove flight / Add staff /
    Remove staff). The caller is expected to follow up with
    ``staged_overrides.apply_staged_overrides`` so the change shows on
    the dashboard immediately.

    Unknown payload keys are dropped rather than stored, so a typo in a
    form field can't quietly become a column nothing reads.
    """
    from ...schemas import STAGED_FORM_OVERRIDE_TYPES
    valid = {t.value for t in STAGED_FORM_OVERRIDE_TYPES}
    if op_type not in valid:
        raise ValueError(
            f"unknown op_type: {op_type!r}. Valid: {sorted(valid)}"
        )
    row: dict[str, str] = {"type": op_type}
    for key, value in payload.items():
        k = str(key).strip().lower()
        if k in OVERRIDE_HEADERS and str(value).strip():
            row[k] = str(value).strip()
    return state.add_override(row)

