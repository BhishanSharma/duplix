"""On-disk copy of the uploaded rosters, so they survive a restart.

The two roster files (staff, AM/ZC) are published once per period —
roughly monthly, sometimes a little longer or shorter — and the operator
should not have to re-upload them every time the server is started. Each
upload is therefore also written to ``data/rosters/`` and reloaded when
the server boots.

Layout::

    data/rosters/
        index.json                       one entry per stored roster
        staff_roster_<stamp>_<id>.xlsx   the file exactly as uploaded
        am_roster_<stamp>_<id>.xlsx

``index.json`` keeps what the UI needs without re-parsing the workbooks:
the original filename, the upload time and the list of dates the roster
has columns for. Which roster answers a given allocation date is decided
from that date list (see ``AppState.roster_files_for``).

Persistence is best-effort: if the folder is read-only the app keeps
working from memory and logs a warning. Override the location with the
``FLIGHT_ALLOC_DATA_DIR`` environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date as date_t
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("src.web")

_REPO_ROOT = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    override = os.environ.get("FLIGHT_ALLOC_DATA_DIR")
    return Path(override) if override else _REPO_ROOT / "data"


def _roster_dir() -> Path:
    return data_dir() / "rosters"


@dataclass
class StoredRoster:
    """One roster as read back from disk."""

    id: str
    kind: str
    filename: str
    uploaded_at: datetime
    dates: list[date_t]
    data: bytes


def _index_path() -> Path:
    return _roster_dir() / "index.json"


def _read_index() -> list[dict]:
    path = _index_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[rosters] cannot read %s: %s", path, exc)
        return []
    return raw if isinstance(raw, list) else []


def _write_index(entries: list[dict]) -> None:
    """Atomic replace, so a crash mid-write can't leave a torn index."""
    root = _roster_dir()
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)
        os.replace(tmp, _index_path())
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _blob_name(entry: dict) -> str:
    return entry["stored_as"]


def save(
    *, file_id: str, kind: str, filename: str, uploaded_at: datetime,
    dates: list[date_t], data: bytes,
) -> None:
    """Write the workbook and add (or replace) its index entry."""
    try:
        root = _roster_dir()
        root.mkdir(parents=True, exist_ok=True)
        stored_as = f"{kind}_{file_id}.xlsx"
        (root / stored_as).write_bytes(data)
        entries = [e for e in _read_index() if e.get("id") != file_id]
        entries.append({
            "id": file_id,
            "kind": kind,
            "filename": filename,
            "uploaded_at": uploaded_at.isoformat(timespec="seconds"),
            "dates": [d.isoformat() for d in sorted(dates)],
            "stored_as": stored_as,
        })
        _write_index(entries)
    except OSError as exc:
        logger.warning(
            "[rosters] could not persist %s (%s) — it stays in memory only",
            filename, exc,
        )


def delete(file_id: str) -> None:
    try:
        entries = _read_index()
        keep = [e for e in entries if e.get("id") != file_id]
        for e in entries:
            if e.get("id") == file_id:
                try:
                    (_roster_dir() / _blob_name(e)).unlink()
                except (OSError, KeyError):
                    pass
        if len(keep) != len(entries):
            _write_index(keep)
    except OSError as exc:
        logger.warning("[rosters] could not delete %s from disk: %s", file_id, exc)


def load_all() -> list[StoredRoster]:
    """Every roster on disk, skipping entries whose file has gone."""
    out: list[StoredRoster] = []
    for e in _read_index():
        try:
            blob = (_roster_dir() / _blob_name(e)).read_bytes()
            out.append(StoredRoster(
                id=str(e["id"]),
                kind=str(e["kind"]),
                filename=str(e["filename"]),
                uploaded_at=datetime.fromisoformat(e["uploaded_at"]),
                dates=[date_t.fromisoformat(d) for d in e.get("dates", [])],
                data=blob,
            ))
        except (OSError, KeyError, ValueError) as exc:
            logger.warning("[rosters] skipping unreadable entry %r: %s", e, exc)
    return out
