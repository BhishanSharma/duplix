"""On-disk Zone Controller list, one list per operating date.

The rosters no longer carry ``/ZC`` in their cells; the assigner picks
the Zone Controllers on the dashboard instead. That choice has to
outlive a restart and has to be *updatable day to day* rather than
re-entered from scratch every morning, so it is kept here.

Rules
-----
* Each date has its own list. Editing a date never touches another one.
* A date with no list of its own **inherits** the list of the most
  recent earlier date ("carried over"). The assigner opens the day,
  sees yesterday's ZCs pre-filled, and only adds / removes what changed.
* The list a day is actually planned with is frozen onto that day (see
  ``freeze``), so later edits to an earlier date cannot rewrite history.
* A date whose list was deliberately emptied stays empty — it does not
  fall back to an older date.
* Removing someone also records them as *removed* for that date only.
  That is what lets the assigner take a ZC off who was marked ``/ZC`` in
  the roster file: they are not in the saved list, so without the record
  there would be nothing to say "not a ZC today". Removals never carry
  over, because a roster ``/ZC`` is a fact about one specific day.

Layout::

    data/zc_lists.json
        {"2026-09-19": {"names": ["NAME ONE", "NAME TWO"],
                        "removed": ["NAME THREE"]}, ...}

(An older file that stores a bare list per date is still read.)

Names are stored as displayed and matched case- and whitespace-
insensitively, the same way override rows match names.

Persistence is best-effort, like ``roster_store``: if the folder is
read-only the lists keep working from memory and a warning is logged.
Override the location with ``FLIGHT_ALLOC_DATA_DIR``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import date as date_t
from pathlib import Path

from .roster_store import data_dir

logger = logging.getLogger("src.web")

_lock = threading.RLock()
_cache: dict[str, dict[str, list[str]]] | None = None
_cache_path: Path | None = None


def _path() -> Path:
    return data_dir() / "zc_lists.json"


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().upper()


def _clean(names: list[str]) -> list[str]:
    """Trim, drop blanks, drop duplicates (case-insensitive), keep order."""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        display = re.sub(r"\s+", " ", str(n or "")).strip()
        key = _norm(display)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(display)
    return out


def _load() -> dict[str, dict[str, list[str]]]:
    """The whole store, read from disk once per path (and again if
    ``FLIGHT_ALLOC_DATA_DIR`` changes)."""
    global _cache, _cache_path
    path = _path()
    with _lock:
        if _cache is not None and _cache_path == path:
            return _cache
        data: dict[str, dict[str, list[str]]] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("[zc] cannot read %s: %s", path, exc)
                raw = {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        date_t.fromisoformat(str(k))
                    except ValueError:
                        continue
                    if isinstance(v, list):          # older format
                        data[str(k)] = {"names": _clean([str(x) for x in v]), "removed": []}
                    elif isinstance(v, dict):
                        data[str(k)] = {
                            "names": _clean([str(x) for x in v.get("names", [])]),
                            "removed": _clean([str(x) for x in v.get("removed", [])]),
                        }
        _cache, _cache_path = data, path
        return data


def _write(data: dict[str, dict[str, list[str]]]) -> None:
    """Atomic replace, so a crash mid-write can't leave a torn file."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dict(sorted(data.items())), fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning(
            "[zc] could not persist the ZC list (%s) — it stays in memory only",
            exc,
        )


def effective(d: date_t) -> tuple[list[str], str, date_t | None]:
    """The list to use for ``d``.

    Returns ``(names, source, carried_from)`` where ``source`` is
    ``"saved"`` (the date has its own list), ``"carried"`` (inherited
    from ``carried_from``) or ``"none"`` (nothing saved on or before
    ``d``).
    """
    with _lock:
        data = _load()
        own = data.get(d.isoformat())
        if own is not None:
            return list(own["names"]), "saved", None
        earlier = [k for k in data if k < d.isoformat()]
        if earlier:
            latest = max(earlier)
            return list(data[latest]["names"]), "carried", date_t.fromisoformat(latest)
        return [], "none", None


def removed(d: date_t) -> list[str]:
    """People explicitly taken off ZC on ``d`` (this date only, never
    carried over)."""
    with _lock:
        own = _load().get(d.isoformat())
        return list(own["removed"]) if own else []


def set_list(
    d: date_t, names: list[str], *, removed_names: list[str] | None = None,
) -> list[str]:
    """Save ``names`` as the list for ``d`` (an empty list is a real,
    deliberate answer and is kept). ``removed_names`` replaces the day's
    removal record; left as ``None`` the existing one is kept, minus
    anyone who is being put on the list."""
    with _lock:
        data = _load()
        cleaned = _clean(names)
        on_list = {_norm(n) for n in cleaned}
        base = data.get(d.isoformat(), {}).get("removed", []) if removed_names is None else removed_names
        data[d.isoformat()] = {
            "names": cleaned,
            "removed": [n for n in _clean(base) if _norm(n) not in on_list],
        }
        _write(data)
        return list(cleaned)


def add(d: date_t, name: str) -> list[str]:
    """Add one person to ``d``'s list, starting from what ``d`` already
    resolves to (so a carried-over list is seeded, then saved)."""
    with _lock:
        names, _, _ = effective(d)
        return set_list(d, names + [name])


def remove(d: date_t, name: str) -> list[str]:
    """Take one person off ``d``'s list, and record them as removed for
    that date so a roster ``/ZC`` for them is overridden too."""
    with _lock:
        names, _, _ = effective(d)
        key = _norm(name)
        return set_list(
            d, [n for n in names if _norm(n) != key],
            removed_names=removed(d) + [name],
        )


def freeze(d: date_t) -> bool:
    """Pin an inherited list onto ``d`` itself. Called when the list is
    actually applied to a plan, so tomorrow's carry-over starts from
    what today really ran with. Returns True when something was written."""
    with _lock:
        names, source, _ = effective(d)
        if source != "carried":
            return False
        set_list(d, names)
        return True


def _reset_for_tests() -> None:
    """Drop the in-memory cache (tests point the store at a temp dir)."""
    global _cache, _cache_path
    with _lock:
        _cache, _cache_path = None, None
