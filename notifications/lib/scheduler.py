# vim: filetype=python
"""Persistent store of scheduled callback notifications, keyed by session id.

Proof-of-concept persistence for the notifications plugin. Each scheduled
callback is a small JSON file under a per-session directory, so that when every
Claude session is closed and the stdio MCP servers die, the schedule survives on
disk. On restart each server recovers the callbacks for *its* recovered session
id (see ../lib/session_state.py) and the dispatcher fires any that are due,
including ones that came due while nothing was running.

stdlib only; the MCP server adds the actual channel delivery on top.
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def store_dir() -> Path:
    """Root directory for persisted callbacks (survives across sessions)."""
    base = os.environ.get("CLAUDE_PLUGIN_DATA")
    root = Path(base) if base else Path.home() / ".claude" / "notifications"
    return root / "scheduled"


def _session_dir(session_id: str) -> Path:
    return store_dir() / _UNSAFE.sub("_", session_id)


def schedule(
    session_id: str,
    due_at: float,
    *,
    kind: str = "scheduled",
    content: str | None = None,
) -> str:
    """Persist a callback for `session_id` due at epoch `due_at`; return its id."""
    callback_id = uuid.uuid4().hex
    directory = _session_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)

    entry: dict[str, object] = {
        "id": callback_id,
        "session_id": session_id,
        "created_at": time.time(),
        "due_at": due_at,
        "kind": kind,
    }
    if content:
        entry["content"] = content

    path = directory / f"{callback_id}.json"
    tmp = path.with_name(f".{callback_id}.json.tmp")
    tmp.write_text(json.dumps(entry))
    os.replace(tmp, path)
    return callback_id


def pending(session_id: str) -> list[dict]:
    """All persisted callbacks for `session_id`, oldest file first."""
    directory = _session_dir(session_id)
    if not directory.is_dir():
        return []
    entries: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            entries.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return entries


def due_callbacks(session_id: str, now: float) -> list[dict]:
    """Callbacks for `session_id` whose due time has passed."""
    return [e for e in pending(session_id) if float(e.get("due_at", 0)) <= now]


def mark_dispatched(entry: dict) -> None:
    """Remove a callback once delivered (callbacks are one-shot)."""
    session_id = entry.get("session_id")
    callback_id = entry.get("id")
    if not session_id or not callback_id:
        return
    path = _session_dir(str(session_id)) / f"{callback_id}.json"
    try:
        path.unlink()
    except OSError:
        pass
