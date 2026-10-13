"""Who was freed early on which date — so it rotates.

When a shift has more people than there are relievers, the surplus staff
are released an hour before shift end instead (see
``allocator/relievers.py``). That must not land on the same people day
after day, so every run records who was released, and the next run
prefers whoever was released least over the last ``window_days``.

Layout::

    data/early_release_log.json
        {"2026-09-26": ["10001", "13819"], ...}

One entry per date; re-running a date overwrites that date's entry (and
the count used for a date never includes the date itself), so
re-allocating the same day does not skew the rotation.

Best-effort like ``zc_store`` / ``roster_store``: if the folder is
read-only, planning still works (the per-day pseudo-random tie-break
still rotates people), it just loses the memory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections import Counter
from collections.abc import Iterable
from datetime import date as date_t
from datetime import timedelta
from pathlib import Path

from .roster_store import data_dir

logger = logging.getLogger("src.web")

_lock = threading.RLock()
_KEEP_DAYS = 120


def _path() -> Path:
    return data_dir() / "early_release_log.json"


def _load() -> dict[str, list[str]]:
    p = _path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("early-release log unreadable (%s); ignoring it", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): [str(x) for x in v]
        for k, v in raw.items()
        if isinstance(v, list)
    }


def history_counts(before: date_t, window_days: int = 30) -> dict[str, int]:
    """``{employee_id: times released early}`` in the ``window_days``
    days strictly before ``before``."""
    lo = before - timedelta(days=window_days)
    out: Counter[str] = Counter()
    with _lock:
        for k, ids in _load().items():
            try:
                d = date_t.fromisoformat(k)
            except ValueError:
                continue
            if lo <= d < before:
                out.update(ids)
    return dict(out)


def record(day: date_t, employee_ids: Iterable[str]) -> None:
    """Store who was released early on ``day`` (replacing any earlier
    record for that date)."""
    ids = sorted({str(x) for x in employee_ids})
    with _lock:
        data = _load()
        data[day.isoformat()] = ids
        cutoff = day - timedelta(days=_KEEP_DAYS)
        for k in list(data):
            try:
                if date_t.fromisoformat(k) < cutoff:
                    del data[k]
            except ValueError:
                del data[k]
        p = _path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
            os.replace(tmp, p)
        except OSError as exc:
            logger.warning("could not save early-release log: %s", exc)
