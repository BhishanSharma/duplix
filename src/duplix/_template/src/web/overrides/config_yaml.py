"""Shared handle on ``configs/config.yml`` for the drawer's write-through
edits.

The edits here are targeted line splices rather than a YAML round-trip,
so the file's commentary survives a save. Every writer takes
``config_write_lock`` first — two drawer forms submitted back to back
land on different request threads.
"""

from __future__ import annotations

import threading
from pathlib import Path

#: Absolute path, so a write works whatever CWD the server was launched from.
CONFIG_YML = Path(__file__).resolve().parents[3] / "configs" / "config.yml"

#: Serialises every read-modify-write against CONFIG_YML.
config_write_lock = threading.Lock()

__all__ = ["CONFIG_YML", "config_write_lock"]
