"""Small, generic JSON-backed key-value store.

Deliberately independent of ``paper_trail.cache`` (which backs the KBART
URL-discovery pipeline): this module is parameterized by path, so callers
each get their own on-disk file rather than sharing state with unrelated
parts of the project. It's thread-safe for the read-modify-write pattern
used by concurrent callers such as ``organizer.py``.
"""

import json
import threading
from pathlib import Path

_lock = threading.Lock()


def load_json_store(path: Path) -> dict:
    """Loads a JSON object from disk, returning ``{}`` if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def update_json_store(
    path: Path, key: str, value: dict, store: dict | None = None
) -> None:
    """Sets a single key in a JSON-backed store and writes it back to disk.

    Guarded by a lock so concurrent threads read-modify-write safely instead
    of racing and silently dropping each other's updates.

    If the caller already has the store loaded in memory (e.g. loaded once
    at the start of a batch run rather than reloading per item), pass it as
    ``store`` to skip re-reading the whole file from disk on every single
    update — the file is still rewritten each call for crash-safety, but
    the wasted read-and-reparse of an ever-growing file is skipped.
    """
    with _lock:
        if store is None:
            store = load_json_store(path)
        store[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(store, f, indent=2)
        except Exception:
            pass
