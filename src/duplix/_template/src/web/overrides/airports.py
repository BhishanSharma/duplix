"""The drawer's "Add international airport code" action.

Appends to ``io.sv_portal.international_airport_codes`` in
``configs/config.yml`` so the engine picks the code up on the next run
with no manual edit step. Validation is split out from the write so the
caller can preview the diff before anything touches the file.
"""

from __future__ import annotations

import re

import yaml

from .config_yaml import CONFIG_YML, config_write_lock

_INTL_CODE_RE = re.compile(r"^[A-Z]{3}$")


def _read_existing_intl_codes() -> list[str]:
    raw = yaml.safe_load(CONFIG_YML.read_text(encoding="utf-8"))
    return list(
        raw.get("io", {})
        .get("sv_portal", {})
        .get("international_airport_codes", [])
    )


def _read_routing_drop_dep_codes() -> list[str]:
    """Read ``io.sv_portal.routing_drop_dep_codes`` — Gulf-3 airports
    whose departing flights are dropped at Step 1 extraction (Phase 3,
    2026-05-14, Change 1). Validation rejects adding these to the
    international list (they're a different concept — dropped flights
    can't be allocated as INTL).

    Empty list when the key is missing; caller treats that as "no
    exclusions configured."
    """
    raw = yaml.safe_load(CONFIG_YML.read_text(encoding="utf-8"))
    return list(
        raw.get("io", {})
        .get("sv_portal", {})
        .get("routing_drop_dep_codes", [])
    )


def validate_intl_airport(
    code: str, name: str | None = None,
) -> tuple[str, str]:
    """Sanitize + validate a proposed international-airport addition.

    Returns ``(sanitized_code, sanitized_name)``. Raises ``ValueError``
    with a user-facing message on any failure. The three checks are:

      1. Code matches the IATA 3-uppercase-letter convention.
      2. Code is not already in ``international_airport_codes``.
      3. Code is not in ``routing_drop_dep_codes`` (Gulf-3 — AUH/DOH/DXB
         by default). Flights departing from those airports are dropped
         at Step 1 (Phase 3 / Change 1, 2026-05-14), so classifying them
         as INTL would never take effect.

    Pure function: does NOT touch disk. Callers compose it with
    ``_apply_intl_airport_to_yaml`` for the actual mutation, either
    directly (the existing operator route) or via the approval chassis
    (the new agent tool).
    """
    if not isinstance(code, str):
        raise ValueError("code must be a string")
    raw = code.strip()
    # Strict check BEFORE upper-casing: Phase 2 spec rejects lowercase
    # outright (it's an API contract violation — the UI uppercases on
    # input so a lowercase request can only come from a script). This
    # changed 2026-05-13; previous behavior silently upper-cased.
    if not _INTL_CODE_RE.match(raw):
        raise ValueError(
            f"code must be exactly 3 uppercase letters (got {code!r})"
        )
    code = raw  # already validated as uppercase
    existing = {c.upper() for c in _read_existing_intl_codes()}
    if code in existing:
        raise ValueError(f"{code} is already in the international list")
    drop_codes = {c.upper() for c in _read_routing_drop_dep_codes()}
    if code in drop_codes:
        raise ValueError(
            f"{code} is in routing_drop_dep_codes (the Gulf-3 "
            "AUH/DOH/DXB list). Flights departing from those airports "
            "are dropped at Step 1, so adding the code to the INTL list "
            "would have no effect. Use a different code or update "
            "routing_drop_dep_codes directly."
        )
    cleaned_name = ""
    if name:
        cleaned_name = re.sub(r"[^\w\s\-/().,]", "", str(name)).strip()
    return code, cleaned_name


def _apply_intl_airport_to_yaml(code: str, name: str = "") -> None:
    """Insert ``code`` (with optional ``name`` as a trailing comment)
    under ``international_airport_codes`` in ``configs/config.yml``.

    Pure mutation: assumes the caller has already validated. The file
    edit is a targeted line-insertion (not a YAML round-trip) so the
    file's commentary survives. Locates the last existing entry under
    the list header and inserts after it, matching that entry's
    indentation.

    Raises ``ValueError`` if the section can't be found at all — that's
    a structural problem with the config, not a validation error, and
    callers should bubble it up.
    """
    with config_write_lock:
        text = CONFIG_YML.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=False)
        header_re = re.compile(
            r"^\s*international_airport_codes\s*:\s*(#.*)?$"
        )
        item_re = re.compile(r"^(\s*-\s+)[A-Z]{3}\s*(#.*)?$")
        header_idx = -1
        last_item_idx = -1
        item_prefix = "      - "  # fallback if no item present yet
        for i, ln in enumerate(lines):
            if header_re.match(ln):
                header_idx = i
                continue
            if header_idx >= 0:
                m = item_re.match(ln)
                if m:
                    last_item_idx = i
                    item_prefix = m.group(1)
                    continue
                # Stop scanning once we leave the list block: a line
                # at less-or-equal indent than the header, or a new
                # YAML key at the io.sv_portal level.
                stripped = ln.strip()
                if stripped and not ln.startswith(" " * 6):
                    break
        if header_idx < 0:
            raise ValueError(
                "could not find international_airport_codes section in config.yml"
            )
        insert_at = (last_item_idx if last_item_idx >= 0 else header_idx) + 1
        new_line = f"{item_prefix}{code}"
        if name:
            new_line = f"{new_line}  # {name}"
        lines.insert(insert_at, new_line)
        CONFIG_YML.write_text("\n".join(lines) + "\n", encoding="utf-8")


def preview_intl_airport_yaml_diff(code: str, name: str = "") -> str:
    """Return a unified-diff-ish string of what ``_apply_intl_airport_to_yaml``
    would do. Used by the agent approval preview.

    Cheap: just rebuilds the would-be insert line and returns a small
    diff string. Does NOT touch disk; safe to call inside the preview
    builder.
    """
    item_prefix = "      - "
    new_line = f"{item_prefix}{code}"
    if name:
        new_line = f"{new_line}  # {name}"
    return (
        f"--- configs/config.yml (before)\n"
        f"+++ configs/config.yml (after)\n"
        f"@@ io.sv_portal.international_airport_codes @@\n"
        f"+{new_line}\n"
    )


def add_intl_airport_code(code: str, name: str | None = None) -> dict[str, str]:
    """Validate + mutate in one shot. Existing direct-write entry point
    (kept for the override drawer's airport form / direct API route).

    The agent tool (``src.agent.tools.add_intl_airport``) uses
    the two underlying functions (``validate_intl_airport`` and
    ``_apply_intl_airport_to_yaml``) separately so the approval chassis
    can preview the diff before any write happens.
    """
    sanitized_code, sanitized_name = validate_intl_airport(code, name)
    _apply_intl_airport_to_yaml(sanitized_code, sanitized_name)
    return {"code": sanitized_code, "name": sanitized_name}

