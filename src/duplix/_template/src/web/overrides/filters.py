"""User-defined extraction filters, stored under the top-level
``extraction_filters:`` key in ``configs/config.yml``.

Each filter is a dict with any subset of {ac_type, ac_owner, ac, dep,
arr, custom_header, custom_value}. Empty fields are skipped during
matching (AND semantics across specified fields, case-insensitive
exact). Writes splice the block in place so surrounding comments live.
"""

from __future__ import annotations

import re

import yaml

from .config_yaml import CONFIG_YML, config_write_lock

#: The fields a filter may specify. Anything else in a submitted dict is
#: dropped by _normalize_filter.
_FILTER_FIELDS = (
    "ac_type", "ac_owner", "ac", "dep", "arr",
    "custom_header", "custom_value",
)




def list_extraction_filters() -> list[dict[str, str]]:
    """Read current extraction filters from config.yml. Only returns
    fields that have a non-empty value — pad-with-empties would
    produce YAML `''` scalars on round-trip and corrupt the file."""
    raw = yaml.safe_load(CONFIG_YML.read_text(encoding="utf-8"))
    filters = raw.get("extraction_filters") or []
    out: list[dict[str, str]] = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        entry: dict[str, str] = {}
        for k in _FILTER_FIELDS:
            v = f.get(k)
            if v is None:
                continue
            sv = str(v).strip()
            if sv:
                entry[k] = sv
        if entry:
            out.append(entry)
    return out


def _normalize_filter(filt: dict[str, str]) -> dict[str, str]:
    """Strip whitespace and uppercase the short codes (airport / type).
    Custom header/value are stripped only (case preserved for header
    since that needs to match the SV-portal column header exactly).
    """
    n: dict[str, str] = {}
    for key in _FILTER_FIELDS:
        v = str(filt.get(key) or "").strip()
        if not v:
            continue
        if key in ("ac_type", "ac_owner", "ac", "dep", "arr"):
            n[key] = v.upper()
        else:
            n[key] = v
    if not n:
        raise ValueError("filter has no fields set — at least one of "
                         f"{_FILTER_FIELDS} must be non-empty")
    # If only one of custom_header / custom_value is set, that's a
    # config error — both required together.
    if ("custom_header" in n) ^ ("custom_value" in n):
        raise ValueError(
            "custom_header and custom_value must be provided together"
        )
    return n


def _yaml_scalar(v: str) -> str:
    """Render a string scalar safely for inline YAML. Plain strings
    are written as-is; if the value contains characters YAML would
    interpret (`:`, `#`, `&`, `*`, leading `-`, etc.) we quote it.
    yaml.safe_dump is NOT used here because it appends a `\n...\n`
    document-end marker for scalars, which corrupts the splice.
    """
    s = str(v)
    needs_quote = (
        s == ""
        or any(c in s for c in ":#&*!|>%@`")
        or s[0] in "-?,[]{}"
        or s.lower() in ("yes", "no", "true", "false", "null", "~", "on", "off")
    )
    if needs_quote:
        # Single-quoted YAML: escape any internal single quotes by doubling.
        escaped = s.replace("'", "''")
        return f"'{escaped}'"
    return s


def _render_filters_block(filters: list[dict[str, str]]) -> str:
    """Format a list of filter dicts as YAML under the top-level
    ``extraction_filters:`` key. Always terminates with a newline.
    """
    if not filters:
        return "extraction_filters: []\n"
    out = "extraction_filters:\n"
    for entry in filters:
        lines = [f"{k}: {_yaml_scalar(v)}" for k, v in entry.items()]
        out += f"  - {lines[0]}\n"
        for ln in lines[1:]:
            out += f"    {ln}\n"
    return out


def _splice_filters_into_config(filters: list[dict[str, str]]) -> None:
    """Write ``filters`` back into ``configs/config.yml``'s
    ``extraction_filters:`` section, leaving every other byte of the
    file intact (comments, whitespace, order)."""
    raw_text = CONFIG_YML.read_text(encoding="utf-8")
    new_block = _render_filters_block(filters).rstrip("\n") + "\n"
    # Match the extraction_filters key + its current value (whether
    # `[]`, `:` followed by a list, or just `:` followed by a blank).
    # Use ^extraction_filters\s*: as anchor; consume up to (but not
    # including) the next top-level key OR end of file.
    pattern = re.compile(
        r"^extraction_filters\s*:[^\n]*\n(?:[ \t-].*\n)*",
        re.MULTILINE,
    )
    if pattern.search(raw_text):
        new_text = pattern.sub(new_block, raw_text, count=1)
    else:
        # Defensive — config.yml ships with the key, so this branch
        # is for upgrades where it was missing.
        new_text = raw_text.rstrip() + "\n\n" + new_block
    CONFIG_YML.write_text(new_text, encoding="utf-8")


def add_extraction_filter(filt: dict[str, str]) -> dict[str, str]:
    """Validate and append one extraction filter to config.yml.
    Returns the normalised dict that was written. Idempotent — a
    duplicate filter is a no-op (same dict returned)."""
    norm = _normalize_filter(filt)
    with config_write_lock:
        existing = list_extraction_filters()
        # Idempotent: skip duplicates (compare non-empty fields only).
        for e in existing:
            if {k: v for k, v in e.items() if v} == norm:
                return norm
        existing.append(norm)
        _splice_filters_into_config(existing)
    return norm


def remove_extraction_filter(idx: int) -> dict[str, str]:
    """Remove the filter at 1-based index ``idx``. Returns the removed
    filter dict. Raises IndexError if out of range."""
    with config_write_lock:
        existing = list_extraction_filters()
        if idx < 1 or idx > len(existing):
            raise IndexError(
                f"extraction_filter index {idx} out of range (1..{len(existing)})"
            )
        removed = existing.pop(idx - 1)
        _splice_filters_into_config(existing)
    return removed


def update_extraction_filter(
    idx: int, new_filter: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Replace the filter at 1-based index ``idx`` with ``new_filter``
    atomically (single config write). Returns (old, new) so the caller
    can render a diff. Raises IndexError if out of range, ValueError
    if the new filter is invalid (e.g. all fields empty)."""
    norm = _normalize_filter(new_filter)
    with config_write_lock:
        existing = list_extraction_filters()
        if idx < 1 or idx > len(existing):
            raise IndexError(
                f"extraction_filter index {idx} out of range (1..{len(existing)})"
            )
        old = existing[idx - 1]
        if {k: v for k, v in old.items() if v} == norm:
            return old, norm   # idempotent — no write needed
        existing[idx - 1] = norm
        _splice_filters_into_config(existing)
    return old, norm


