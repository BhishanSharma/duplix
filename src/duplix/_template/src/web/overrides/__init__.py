"""Override mutations exposed by the web UI's drawer.

Three concerns, three modules:

    rows          the drawer's override table, the recommender's
                  one-click fixes, and the staged add/remove forms
    airports      write-through of a new international airport code
    filters       write-through of the extraction-filter list
    config_yaml   the shared config.yml handle + write lock

``airports`` and ``filters`` both edit ``configs/config.yml`` so the
engine picks the change up on the next run with no manual edit step.
The edits are targeted line splices rather than a YAML round-trip, so
the file's commentary survives.

Import from here — the sub-module split is an implementation detail.
"""

from __future__ import annotations

from .airports import (
    add_intl_airport_code,
    preview_intl_airport_yaml_diff,
    validate_intl_airport,
)
from .config_yaml import CONFIG_YML, config_write_lock
from .filters import (
    add_extraction_filter,
    list_extraction_filters,
    remove_extraction_filter,
    update_extraction_filter,
)
from .rows import (
    add_override,
    apply_phase_r_override,
    clear_all_overrides,
    delete_override,
    update_override,
    write_staged_override,
)

__all__ = [
    "CONFIG_YML",
    "add_extraction_filter",
    "add_intl_airport_code",
    "add_override",
    "apply_phase_r_override",
    "clear_all_overrides",
    "config_write_lock",
    "delete_override",
    "list_extraction_filters",
    "preview_intl_airport_yaml_diff",
    "remove_extraction_filter",
    "update_extraction_filter",
    "update_override",
    "validate_intl_airport",
    "write_staged_override",
]
